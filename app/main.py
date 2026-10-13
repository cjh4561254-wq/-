r"""FastAPI 应用入口。

启动命令（在项目根目录执行）：
    .\.venv\Scripts\Activate.ps1
    python -m uvicorn app.main:app --reload --port 8000

"app.main:app" 的意思是：从 app/main.py 里取出名为 app 的那个对象。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import config
from app.api.routes_health import router as health_router
from app.schemas import ApiError, ApiResponse, ErrorCode


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动 / 关闭时要做的事。

    启动时把上传目录建出来。放在这里而不是写在 send 文件的那一刻：
    目录建不出来属于"环境问题"，应该在启动日志里就喊出来，
    而不是等用户点了上传才报错。
    """
    upload_dir = config.get_settings().upload_path
    upload_dir.mkdir(parents=True, exist_ok=True)
    print(f"[startup] 上传目录就绪: {upload_dir}")
    yield
    # yield 之后是关闭逻辑，目前没有需要清理的资源


app = FastAPI(
    title=config.get_settings().app_name,
    version=config.get_settings().app_version,
    description="多智能体 AI 数据分析系统：上传 CSV/Excel，用自然语言提问，得到真实计算的分析结果与图表。",
    lifespan=lifespan,
)

# 允许本机前端页面跨域调用。
# 只用 127.0.0.1 / localhost，不使用 "*"，避免把接口暴露给任意网站。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",  # VS Code Live Server 常用端口
        "http://localhost:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 统一错误响应 ----------------
# FastAPI 默认的错误格式是 {"detail": ...}，和我们成功的 {"ok":true,"data":...}
# 是两套结构。前端如果只写一套解析逻辑，遇到 404 / 参数错误就会解析失败。
# 下面把框架自己抛的错误也翻译成同一个外壳，前端只需要认一种格式。


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
    # 404 归到 NOT_FOUND，其余归到 BAD_REQUEST。
    # 具体业务错误码（FILE_TOO_LARGE 等）由路由自己返回 ApiResponse，不走这里。
    code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.BAD_REQUEST
    body = ApiResponse[None](
        ok=False,
        data=None,
        error=ApiError(code=code, message=str(exc.detail), detail=request.url.path),
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))


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
            detail=str(exc.errors()),
        ),
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json"))


# 所有业务路由统一挂在 /api 前缀下
app.include_router(health_router, prefix="/api")


@app.get("/", tags=["meta"], summary="根路径")
def root() -> dict:
    """给人看的入口提示，方便一眼确认服务起来了。"""
    settings = config.get_settings()
    return {
        "ok": True,
        "data": {
            "app": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "health": "/api/health",
        },
        "error": None,
    }
