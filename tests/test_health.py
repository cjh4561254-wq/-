"""第一阶段验收测试：证明服务真的能被加载并正确响应。

这里没有启动 uvicorn，也没有占用 8000 端口。
TestClient 直接把 HTTP 请求"喂"给 FastAPI 应用对象，走完整的路由和
响应模型校验流程，只是跳过了网络层。所以它能测出真实的接口逻辑错误。

一条重要约定：测试**不允许**假设 .env 是空的。
否则第 4 天你填上真实密钥后，测试会集体变红 —— 那是测试写错了，不是代码错了。
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.main import app
from app.schemas import ErrorCode

client = TestClient(app)

# 大模型相关配置的三个来源：真实环境变量、.env 文件、代码默认值。
# 下面的 fixture 把前两个都按住，这样"未配置基线"才是真的未配置。
LLM_ENV_VARS = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")


@pytest.fixture
def clean_settings_factory(monkeypatch):
    """返回一个"绝对干净"的 Settings 构造器：不读环境变量、不读 .env。

    为什么需要它？
        monkeypatch.delenv 清掉当前进程的环境变量；_env_file=None 让
        pydantic-settings 不去读 .env 文件。两者缺一不可 ——
        只做其中一个，你本地填过密钥之后这个测试照样会红。
    """

    def _build(**overrides) -> config.Settings:
        for name in LLM_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        return config.Settings(_env_file=None, **overrides)

    return _build


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
    assert data["app_name"] == config.get_settings().app_name
    assert data["version"] == config.get_settings().app_version
    assert data["uptime_seconds"] >= 0
    # 上传目录由 lifespan 启动时创建，TestClient 会跑 lifespan，所以这里必须是 True
    assert data["upload_dir_ready"] is True


def test_health_matches_config_regardless_of_env():
    """接口报告的 llm_configured 必须等于配置对象的真实状态。

    注意断言方式是 `接口 == 配置`，而不是 `配置 == False`。
    这样无论 .env 填没填，测试都成立 —— 它测的是"接口有没有如实汇报"。
    """
    settings = config.get_settings()
    assert client.get("/api/health").json()["data"]["llm_configured"] == settings.llm_configured


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
    settings = config.get_settings()
    assert settings.max_upload_bytes == settings.max_upload_mb * 1024 * 1024
    assert settings.max_upload_bytes == 10 * 1024 * 1024


def test_api_key_never_shown_in_repr(clean_settings_factory):
    """安全底线：密钥不能通过 print(settings) 泄漏到日志里。"""
    secret = "sk-this-is-a-fake-secret-for-testing"
    probe = clean_settings_factory(
        llm_api_key=secret,
        llm_base_url="https://api.example.com/v1",
        llm_model="example-model",
    )
    assert probe.llm_api_key == secret   # 程序内部能拿到真实值
    assert secret not in repr(probe)     # 但打印时看不到
    assert probe.llm_configured is True


def test_llm_configured_requires_all_three_fields(clean_settings_factory):
    """少任何一项都算没配好：只填一半会在真正调用时报错，不如提前判断。"""
    assert clean_settings_factory().llm_configured is False
    assert clean_settings_factory(llm_base_url="https://x/v1").llm_configured is False
    assert clean_settings_factory(llm_base_url="https://x/v1", llm_model="m").llm_configured is False
    assert (
        clean_settings_factory(
            llm_base_url="https://x/v1", llm_model="m", llm_api_key="k"
        ).llm_configured
        is True
    )


def test_invalid_upload_limit_is_rejected(clean_settings_factory):
    """配置校验要真的起作用：0 或 999 MB 都应该被 Pydantic 挡下来。"""
    with pytest.raises(ValidationError):
        clean_settings_factory(max_upload_mb=0)
    with pytest.raises(ValidationError):
        clean_settings_factory(max_upload_mb=999)


def test_settings_cache_can_actually_be_reset(monkeypatch):
    """这是第一阶段被 QA 抓出来的阻塞 bug 的回归测试。

    旧写法在模块顶部 `settings = get_settings()`，业务模块 `from app.config
    import settings` 把它绑死在 import 时刻。结果是：测试改了环境变量、
    调了 cache_clear()，业务模块用的却还是旧对象 —— 测试"看着绿，其实没测到"。

    现在业务模块改为每次 config.get_settings() 现取。
    本测试锁死这个行为：重置缓存 + 改环境变量后，必须能读到新值。
    """
    monkeypatch.setenv("LLM_MODEL", "model-from-test")
    config.reset_settings_cache()
    try:
        assert config.get_settings().llm_model == "model-from-test"
        # 接口是现取配置的，所以它也必须跟着变
        assert client.get("/api/health").json()["data"]["version"] == config.get_settings().app_version
    finally:
        config.reset_settings_cache()  # 还原，避免影响其他测试


# ---------------- 统一错误格式 ----------------


def test_unknown_path_uses_unified_error_envelope():
    """不存在的接口返回 404，而且必须是统一外壳，不能是 FastAPI 默认的 {"detail": ...}。"""
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404

    body = resp.json()
    assert set(body.keys()) == {"ok", "data", "error"}
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == ErrorCode.NOT_FOUND.value
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_wrong_method_uses_unified_error_envelope():
    """用错 HTTP 方法（对 /api/health 发 POST）也走统一外壳。"""
    resp = client.post("/api/health")
    assert resp.status_code == 405
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] is not None
