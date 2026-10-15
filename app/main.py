r"""FastAPI 应用入口。

启动命令（在项目根目录执行）：
    .\.venv\Scripts\Activate.ps1
    python -m uvicorn app.main:app --reload --port 8000

"app.main:app" 的意思是：从 app/main.py 里取出名为 app 的那个对象。
"""

from contextlib import asynccontextmanager
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.api.routes_analysis import router as analysis_router
from app.api.routes_datasets import router as datasets_router
from app.api.routes_health import router as health_router
from app.api.routes_workflows import router as workflows_router
from app.data.history import get_history_store
from app.errors import AppError, sanitize_detail
from app.schemas import ApiError, ApiResponse, ErrorCode
from app.runtime import runtime_environment

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭时要做的事。

    启动时把上传目录建出来。放在这里而不是写在 send 文件的那一刻：
    目录建不出来属于"环境问题"，应该在启动日志里就喊出来，
    而不是等用户点了上传才报错。

    归档库（§14 决策 3）也在这里初始化：建表幂等，启动做一次，
    之后每个连接就不必再判断表在不在。
    """
    settings = config.get_settings()
    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    with runtime_environment(settings) as temp_dir:
        print(f"[startup] 上传目录就绪: {upload_dir}")
        print(f"[startup] 临时目录就绪: {temp_dir}")
        _prepare_history_store()
        yield
    # 退出上下文管理器，恢复进程原有的环境变量与 tempfile 缓存。


def _prepare_history_store() -> None:
    """建归档库的表（归档关闭时什么都不做）。

    **这里的异常必须吞掉，只记 warning。** 两条可用性的优先级不一样：
    归档是旁路功能 —— 历史列表打不开、库文件建不出来，用户照样能上传、提问、
    看图表和报告；而启动失败是**整个服务不可用**，连页面都打不开。
    拿后者去赌前者是不划算的（对比 `app/core/archive.py`：归档写入失败也只记 warning）。
    真正的故障不会因此消失：第一次归档失败时日志里会出现一条带 workflow_id 的 warning，
    这里则留下"归档库初始化失败"这一条，排查时两处对得上。
    """
    try:
        store = get_history_store()
        if store is None:
            # `DB_PATH` 为空串 = 用户主动关掉归档，不是错误。
            print("[startup] 归档已关闭（DB_PATH 为空），历史接口返回 enabled=false")
            return
        store.ensure_schema()
        print(f"[startup] 归档库就绪: {store.path}")
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：可用性优先于归档可用性
        logger.warning(
            "归档库初始化失败，服务继续启动（历史列表将不可用）：%s: %s",
            type(exc).__name__,
            exc,
        )


app = FastAPI(
    title=config.get_settings().app_name,
    version=config.get_settings().app_version,
    description="多智能体 AI 数据分析系统：上传 CSV/Excel，用自然语言提问，得到真实计算的分析结果与图表。",
    lifespan=lifespan,
)

# 允许本机前端页面跨域调用。
# 只用 127.0.0.1 / localhost，不使用 "*"，避免把接口暴露给任意网站。
# 端口为什么列了 8000 又列 8128：README 里文档化的启动端口是 8128
# （8000 在本机被另一个项目占着，实测绑不上），但 8000 仍是历史文档与
# 早期验收记录里写的端口，所以两个都留着 —— 多一个本机来源不放宽任何安全边界。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8128",
        "http://localhost:8128",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",  # VS Code Live Server 常用端口
        "http://localhost:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 前端静态资源 ----------------
# 页面和资源都在 app/web/ 下。目录用 PROJECT_ROOT 算，不看启动时的 cwd ——
# 从任何目录起 uvicorn 都能找到页面（与 app/config.py 的 upload_path 同一套做法）。
#
# **只挂 app/web，data/ 绝不挂成静态资源。**
# data/uploads 里是用户上传的原始文件，挂上去就等于把它们变成公开 URL；
# data/samples 里的示例数据同理。图表一律走接口 8（/api/workflows/{id}/charts/{file}），
# 这条纪律在 docs/ARCHITECTURE.md §9.3 里写死了，tests/test_frontend.py 有断言盯着。
WEB_DIR = config.PROJECT_ROOT / "app" / "web"
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

# ---------------- 统一错误响应 ----------------
# FastAPI 默认的错误格式是 {"detail": ...}，和我们成功的 {"ok":true,"data":...}
# 是两套结构。前端如果只写一套解析逻辑，遇到 404 / 参数错误就会解析失败。
# 下面把框架自己抛的错误、以及我们自己的 AppError 都翻译成同一个外壳，
# 前端只需要认一种格式。
#
# 两个概念不要混：
#   HTTP 状态码 —— 由 handler 原样透传（404 就是 404，413 就是 413）
#   error.code  —— 业务语义，由 AppError.code 决定（见 app/errors.py）
# 细节和完整映射表都写在 app/errors.py 的模块注释里。


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """把业务错误（AppError）翻译成统一外壳。

    业务代码里只写 `raise AppError(ErrorCode.NOT_FOUND, "数据集不存在")`，
    状态码由 AppError.status_code 决定（它来自 app/errors.py 的 ERROR_STATUS 表）。
    """
    body = ApiResponse[None](
        ok=False,
        data=None,
        error=ApiError(
            code=exc.code,
            message=exc.message,
            detail=sanitize_detail(exc.detail) if exc.detail else request.url.path,
        ),
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


# 框架抛的状态码 → 业务错误码。
# 为什么 405 不能也归到 BAD_REQUEST？因为前端会按 error.code 决定文案：
# 归到 BAD_REQUEST 会让"你用错了 HTTP 方法"提示成"请检查你的输入"，
# 而用户真正看到的多半是我们自己前端写错了 URL —— 提示指错了方向，
# 排查时就得多绕一圈。
_FRAMEWORK_STATUS_CODES: dict[int, ErrorCode] = {
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """把 HTTPException 翻译成统一外壳。

    这里捕获的是 Starlette 的 HTTPException，而不是 FastAPI 的 ——
    因为 FastAPI 的 HTTPException 是它的子类，而 404 / 405 是 Starlette
    内部抛的基类异常。只注册子类的话，404 会漏掉，仍然返回 {"detail": ...}。
    （第一版就是这么写错的，被 tests/test_error_format.py 抓了出来。）

    路由里可以写 `raise HTTPException(status_code=404, detail="没有这个数据集")`，
    客户端拿到的仍然是标准失败格式。
    """
    # FastAPI / Starlette 自己抛的这几个状态码，翻译成最接近的业务码。
    # 需要精确业务码（FILE_TOO_LARGE / UNSUPPORTED_FILE 等）时用 AppError，
    # 它的状态码会跟着错误码一起变（见 app/errors.py 的 ERROR_STATUS 表）。
    code = _FRAMEWORK_STATUS_CODES.get(exc.status_code, ErrorCode.BAD_REQUEST)
    body = ApiResponse[None](
        ok=False,
        data=None,
        error=ApiError(
            code=code,
            message=str(exc.detail),
            detail=sanitize_detail(request.url.path),
        ),
    )
    # exc.headers 必须转发：405 的 Allow 头（RFC 7231 §6.5.5 要求）和
    # 401 的 WWW-Authenticate 都挂在这里。自定义 handler 里漏掉它，
    # 客户端就再也拿不到这些头了 —— 这类"静默丢失"只有对着原始响应头看才发现。
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json"),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体 / 查询参数不合法（默认是 422）。

    对学生最有用的一点：把"哪个字段错了"原样带回去，
    而不是只丢一句 "Unprocessable Entity"。
    """
    body = ApiResponse[None](
        ok=False,
        data=None,
        error=ApiError(
            code=ErrorCode.BAD_REQUEST,
            message="请求参数校验失败",
            detail=sanitize_detail(str(exc.errors())),
        ),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何没被上面三个 handler 接住的异常，也返回统一外壳。

    为什么需要它？
        权限不一致、文件句柄没关、第三方库抛出的意外异常……这些都会走到这里。
        没有这个 handler 时，FastAPI 会返回纯文本 "Internal Server Error"，
        前端 json() 解析直接抛异常，页面上只会看到一个转圈或者空白 ——
        用户拿不到任何有用信息，我们也不知道发生了什么。

    三条纪律：
        ① HTTP 仍然是 500，绝不为了"好看"改成 200；
        ② detail 清洗掉本机路径，并且截断到 500 字符，避免把整个堆栈丢给客户端；
        ③ 控制台打印完整异常（traceback），**排查信息留在服务端日志里**，
           不要放在响应体里 —— 响应体会被截图、会被贴进 issue。
    """
    print(f"[error] 未预期的异常 {request.method} {request.url.path}: {exc!r}")
    traceback.print_exc()

    body = ApiResponse[None](
        ok=False,
        data=None,
        error=ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="服务器内部错误，请查看服务端日志",
            detail=sanitize_detail(str(exc))[:500] if str(exc) else None,
        ),
    )
    return JSONResponse(status_code=500, content=body.model_dump(mode="json"))


# 所有业务路由统一挂在 /api 前缀下
app.include_router(health_router, prefix="/api")
app.include_router(datasets_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")
app.include_router(workflows_router, prefix="/api")


@app.get("/", tags=["meta"], summary="分析页面")
def root() -> FileResponse:
    """首页：返回前端单页 `app/web/index.html`。

    为什么不再返回一段 JSON 提示：
        这个项目的入口本来就是"上传数据 → 提问 → 看报告"这一个页面，
        返回 JSON 还得让人自己去拼地址。原来 JSON 里的几个入口
        （`/docs`、`/api/health`）现在放在页面页脚里，地址一个都没少。

    为什么用 FileResponse 而不是 StaticFiles(html=True) 挂根路径：
        挂根路径会把整个 app/web/ 变成"任何文件都能按路径取"的静态站，
        以后往里放任何东西都会自动对公网可见。这里只放出明确的一个文件。
    """
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")
