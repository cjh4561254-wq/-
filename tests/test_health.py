"""第一阶段验收测试：证明服务真的能被加载并正确响应。

这里没有启动 uvicorn，也没有占用 8000 端口。
TestClient 直接把 HTTP 请求"喂"给 FastAPI 应用对象，走完整的路由和
响应模型校验流程，只是跳过了网络层。所以它能测出真实的接口逻辑错误。
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.main import app

client = TestClient(app)


# ---------------- 健康检查 ----------------


def test_health_returns_200():
    """最基本的连通性：接口存在且返回 200。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_health_envelope_shape():
    """统一响应外壳的三个键必须齐全，且 ok=True 时 error 必须为 null。"""
    body = client.get("/api/health").json()
    assert set(body.keys()) == {"ok", "data", "error"}
    assert body["ok"] is True
    assert body["error"] is None

    data = body["data"]
    assert data["status"] == "healthy"
    assert data["app_name"] == settings.app_name
    assert data["version"] == settings.app_version
    assert data["uptime_seconds"] >= 0


def test_health_reports_llm_not_configured_without_env():
    """当前还没填 .env，所以必须如实报告"未配置大模型"。

    如果哪天这个测试挂了，说明 .env 被填上了：
    那时应该改成显式断言，而不是删掉这个测试。
    """
    assert settings.llm_configured is False
    assert client.get("/api/health").json()["data"]["llm_configured"] is False


# ---------------- 根路径 ----------------


def test_root_hint():
    """根路径给出文档入口，方便手动确认服务状态。"""
    body = client.get("/").json()
    assert body["ok"] is True
    assert body["data"]["docs"] == "/docs"
    assert body["data"]["health"] == "/api/health"


# ---------------- 配置层 ----------------


def test_max_upload_bytes_conversion():
    """10 MB 必须换算成 10485760 字节（用 1024 进制，不是 1000）。"""
    assert settings.max_upload_bytes == settings.max_upload_mb * 1024 * 1024
    assert settings.max_upload_bytes == 10 * 1024 * 1024


def test_api_key_never_shown_in_repr():
    """安全底线：密钥不能通过 print(settings) 泄漏到日志里。"""
    secret = "sk-this-is-a-fake-secret-for-testing"
    probe = type(settings)(
        llm_api_key=secret,
        llm_base_url="https://api.example.com/v1",
        llm_model="example-model",
    )
    assert probe.llm_api_key == secret   # 程序内部能拿到真实值
    assert secret not in repr(probe)     # 但打印时看不到
    assert probe.llm_configured is True


def test_invalid_upload_limit_is_rejected():
    """配置校验要真的起作用：0 或 999 MB 都应该被 Pydantic 挡下来。"""
    with pytest.raises(ValidationError):
        type(settings)(max_upload_mb=0)
    with pytest.raises(ValidationError):
        type(settings)(max_upload_mb=999)


# ---------------- 未注册路径 ----------------


def test_unknown_path_returns_404():
    """不存在的接口返回 404，而不是 500 —— 说明路由匹配是干净的。"""
    assert client.get("/api/does-not-exist").status_code == 404
