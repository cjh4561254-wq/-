"""健康检查路由：GET /api/health

作用：
    1. 前端 / 监控 / 你自己确认"服务活着"。
    2. 提前暴露配置错误 —— 比如上传目录建不出来，这里就能发现，
       而不是等到用户上传文件时才炸。
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.runtime import uptime_seconds
from app.schemas import ApiResponse

router = APIRouter(tags=["health"])


class HealthData(BaseModel):
    """data 字段的结构。写出来是为了让它出现在 /docs 文档里。"""

    status: str
    app_name: str
    version: str
    uptime_seconds: float
    llm_configured: bool


@router.get("/health", response_model=ApiResponse[HealthData], summary="健康检查")
def health() -> ApiResponse[HealthData]:
    """返回服务状态与关键配置的自检结果。"""
    return ApiResponse[HealthData](
        ok=True,
        data=HealthData(
            status="healthy",
            app_name=settings.app_name,
            version=settings.app_version,
            uptime_seconds=uptime_seconds(),
            llm_configured=settings.llm_configured,
        ),
    )
