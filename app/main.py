r"""FastAPI 应用入口。

启动命令（在项目根目录执行，端口与 README 一致）：
    .\.venv\Scripts\Activate.ps1
    python -m uvicorn app.main:app --reload --port 8128

"app.main:app" 的意思是：从 app/main.py 里取出名为 app 的那个对象。

端口为什么是 8128 而不是 8000：本机 8000 被另一个项目占着，照旧文档敲会
`[winerror 10013]` 绑不上（真实报错原文记在 README 的"启动"一节）。
`scripts/demo.py` 用的是同一个端口，所以自动演示与手动启动不会互相打架。
"""

from contextlib import asynccontextmanager
import logging
import traceback

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.api.routes_analysis import router as analysis_router
from app.api.routes_auth import router as auth_router
from app.api.routes_datasets import router as datasets_router
from app.api.routes_health import router as health_router
from app.api.routes_workflows import router as workflows_router
from app.core.auth import current_user_or_none, require_user
from app.data.history import get_history_store
from app.data.users import get_auth_store
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

    两个 SQLite 库（归档 §14 决策 3 / 账号库）也在这里初始化：
    建表幂等，启动做一次，之后每个连接就不必再判断表在不在。
    """
    settings = config.get_settings()
    upload_dir = settings.upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    with runtime_environment(settings) as temp_dir:
        print(f"[startup] 上传目录就绪: {upload_dir}")
        print(f"[startup] 临时目录就绪: {temp_dir}")
        _prepare_history_store()
        _prepare_auth_store()
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


def _prepare_auth_store() -> None:
    """建账号库的表（`AUTH_DB_PATH` 为空时什么都不做）。

    为什么这里**也**吞异常、只记 warning —— 与 `_prepare_history_store()` 的分工不同，
    这一点值得说清楚：

    归档失败只影响"历史列表"，所以那里吞异常是为了**保住启动**。
    认证失败的影响大得多（没人能登录），但这里仍然不抛：
    抛出去的结果是 uvicorn 起不来，用户看到的是**连页面都打不开**，
    比起得来、登录时明确报 503「账号库不可用」，后者的排查信息多得多。

    关键区别在于**认证失败绝不能变成放行**：所以 `app/core/auth.py` 的
    `resolve_session()` 在库不可用时返回 `None`（= 拒绝），而不是 `True`。
    这里让服务起得来，不等于让接口不设防。
    """
    try:
        store = get_auth_store()
        if store is None:
            print("[startup] 账号库已关闭（AUTH_DB_PATH 为空），登录接口将返回 503")
            return
        store.ensure_schema()
        print(f"[startup] 账号库就绪: {store.path}")
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：起得来比起不来好排查
        logger.warning(
            "账号库初始化失败，服务继续启动（登录接口将不可用）：%s: %s",
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
    # exc.headers 必须转发，理由与下面 StarletteHTTPException 那段一样：
    # 401 的 WWW-Authenticate、429 的 Retry-After 都挂在这里。
    # 少了这一句，"登录失败限流"就只能返回裸 429，客户端不知道该等多久。
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json"),
        headers=exc.headers,
    )


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
#
# 认证路由（`/api/auth/*`）**不挂 require_user 依赖**：登录、退出、
# "我现在是谁"、首次初始化，这四个本来就发生在"还没有身份"的时候。
# 它们各自的保护写在自己的路由里（改密码要当前密码、初始化只在空库时可用）。
app.include_router(auth_router, prefix="/api")

# 健康检查**故意不要求登录**：登录页自己要用它显示"服务是否正常"，
# 而它返回的六个字段（状态/应用名/版本/运行时长/大模型是否配置/上传目录是否就绪）
# 没有一个属于某个用户。要求登录才能看健康状态，会让登录页在服务异常时
# 无法告诉用户"是服务挂了还是密码错了"。
app.include_router(health_router, prefix="/api")


# 受保护的业务路由。
#
# **依赖加在 include 这一处，而不是在每个路由函数上写 `user=Depends(require_user)`。**
# 理由：一个接口漏写依赖 = 一个完全裸奔的接口，而"漏写"这件事不会有任何测试
# 主动发现（测试只会测它自己调的那些）。集中成一行列在这里，
# 新增 router 时要么被这一行覆盖，要么会立刻在 code review 里显眼。
#
# 它保护的是**服务端**：前端就算把登录页删掉、把 cookie 手动塞回去，
# 没有有效会话的请求照样拿到 401。前端路由守卫只是体验，不是安全边界。
_PROTECTED = [Depends(require_user)]

app.include_router(datasets_router, prefix="/api", dependencies=_PROTECTED)
app.include_router(analysis_router, prefix="/api", dependencies=_PROTECTED)
app.include_router(workflows_router, prefix="/api", dependencies=_PROTECTED)


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

    **这个路径故意不要求登录**（登录页上的"服务是否正常"也要读得动它，
    而且页面的路由守卫在浏览器里，服务端返回 200 不代表看到数据）。
    真正的安全边界在 `/api/*`：那三个业务 router 挂了 `require_user`，
    没会话的请求一个数据都取不到。
    """
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/login", tags=["meta"], summary="登录页面")
def login_page() -> FileResponse:
    """登录页：返回 `app/web/login.html`。

    为什么不复用 `/`：
        `/` 是应用主体（上传、提问、报告），未登录时打开它只会看到一个空壳，
        而用户真正需要的是"先把密码填了"。分成两个文件，浏览器里的行为
        才是可预期的：未登录 → `/login`，登录成功 → `/app`。

    页面本身要能匿名打开 —— 否则登录页要求登录，就成了死循环。
    所以这个路由**没有** `require_user`。
    """
    return FileResponse(WEB_DIR / "login.html", media_type="text/html")


@app.get("/app", tags=["meta"], summary="应用主页面（需登录）", response_model=None)
def app_page(request: Request) -> FileResponse | RedirectResponse:
    """应用主页：已登录返回 `index.html`，未登录 303 跳 `/login`。

    `response_model=None` 不是可选的美化，是**必须的**：
        这个函数会返回两种响应（`FileResponse` 或 `RedirectResponse`），
        FastAPI 看到返回注解就会拿它生成 response_model，而"Response 的联合类型"
        不是合法的 Pydantic 字段 —— 实测直接抛
        `FastAPIError: Invalid args for response field!`，而且是在
        **import 阶段**，后果是整个应用起不来、21 个测试文件全部收集失败。
        关掉 response_model 之后，FastAPI 就不再从注解推断响应模型，
        这里返回的本来就是完整的 Response 对象，不需要它推断。

    为什么这里不用 `Depends(require_user)`：
        那个依赖抛的是 `AppError(UNAUTHORIZED)`，会被统一异常处理器翻译成
        **401 + JSON**。对 `/api/*` 那是对的（前端 fetch 靠状态码判断），
        但对"在地址栏里敲进来的一次页面导航"就错了：用户会看到一屏 JSON，
        而不是登录页。所以页面路由自己判断，用重定向。

    为什么是 303 而不是 302：
        303 明确要求浏览器把后续请求改用 GET。这个路由本身只处理 GET，
        但 303 让"这里发生的是导航、不是数据请求"这件事在响应里写清楚，
        也避免以后有人给它加 POST 时踩到 302 的语义模糊。

    为什么把 `next` 参数省掉：
        回跳地址一旦由查询串决定，就得防"开放重定向"（`?next=https://…`）。
        这个应用只有一个主页面，登录成功固定回 `/app`，
        不做这个参数就是最省事的正确做法。
    """
    if current_user_or_none(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")
