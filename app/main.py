r"""FastAPI 应用入口。

启动命令（在项目根目录执行）：
    .\.venv\Scripts\Activate.ps1
    python -m uvicorn app.main:app --reload --port 8000

"app.main:app" 的意思是：从 app/main.py 里取出名为 app 的那个对象。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_health import router as health_router
from app.config import settings

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="多智能体 AI 数据分析系统：上传 CSV/Excel，用自然语言提问，得到真实计算的分析结果与图表。",
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

# 所有业务路由统一挂在 /api 前缀下
app.include_router(health_router, prefix="/api")


@app.get("/", tags=["meta"], summary="根路径")
def root() -> dict:
    """给人看的入口提示，方便一眼确认服务起来了。"""
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
