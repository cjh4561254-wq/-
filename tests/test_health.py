"""第一阶段验收测试：证明服务真的能被加载并正确响应。

这里没有启动 uvicorn，也没有占用 8000 端口。
TestClient 直接把 HTTP 请求"喂"给 FastAPI 应用对象，走完整的路由和
响应模型校验流程，只是跳过了网络层。所以它能测出真实的接口逻辑错误。

一条重要约定：测试**不允许**假设本机环境是空的。
否则第 4 天你把真实密钥填进系统环境变量后，测试会集体变红 —— 那是测试写错了，
不是代码错了。（配置层已不读 .env；这条约定现在由 conftest.py 的
`_isolate_llm_env` 夹具强制执行，而不是靠每个文件自己记得清。）
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.main import app
from app.schemas import ErrorCode

client = TestClient(app)

# 注意上面这行**没有**用 `with`，所以 lifespan 不会执行（startup 钩子跑不到）。
# 需要验证"启动时真的建了上传目录"的测试，必须自己写 `with TestClient(app) as c:`。
# 这是被 QA 抓出来的一个假绿测试：旧注释写"TestClient 会跑 lifespan"，
# 而那条断言能过只是因为 data/uploads/.gitkeep 被 git 跟踪、目录碰巧存在。
# 一旦换一台干净机器或改 UPLOAD_DIR，它就红 —— 看起来像代码坏了，其实是测试写错了。

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
    # 这里**只断言类型**，不断言 True：lifespan 没跑时它本来就可能是 False，
    # 那条断言会变成"依赖 checkout 里碰巧有 data/uploads 目录"的假绿。
    # "启动时真的建目录"由下面的 test_lifespan_creates_upload_dir 负责。
    assert isinstance(data["upload_dir_ready"], bool)


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
    调了 cache_clear()，业务模块用的却还是旧对象。

    **这条测试的写法被 QA 返工过一次，值得记住为什么。**

    第一版只断言了 `config.get_settings().llm_model == 新值` 和
    `接口的 version == config.get_settings().app_version`，看起来合理，
    但 QA 拿它在**旧的有 bug 的树上**跑，两条断言全是 True —— 也就是说
    bug 回来了它也照样绿，等于没测。

    原因：旧代码的 config.get_settings 本身也是 lru_cache 函数，
    cache_clear() 之后它自己当然会返回新值（第一条必然成立）；第二条比的是
    两个都来自 get_settings() 的 app_version，同源必然相等。

    要真正区分新旧实现，必须让断言穿过**那个曾经被绑死的引用**：
    改 APP_NAME → 重置缓存 → 问 HTTP 接口。接口拿到的值必须跟着变。
    """
    monkeypatch.setenv("APP_NAME", "renamed-by-test")
    monkeypatch.setenv("LLM_MODEL", "model-from-test")
    config.reset_settings_cache()
    try:
        # ① 配置层本身能读到新值（这条新旧实现都成立，保留它只是先挡住最外层的错）
        assert config.get_settings().llm_model == "model-from-test"

        # ② 关键断言：接口里那次 config.get_settings() 必须是**现取**的。
        #    旧实现下接口返回的是 import 时刻的旧对象，这里会红。
        assert client.get("/api/health").json()["data"]["app_name"] == "renamed-by-test"
        assert client.get("/").json()["data"]["app"] == "renamed-by-test"
    finally:
        # 清缓存让后续测试重新读取 monkeypatch（已还原）的环境变量。
        # 这里不需要手动还原环境变量：monkeypatch 在本测试结束后自动还原。
        config.reset_settings_cache()


def test_lifespan_creates_upload_dir(monkeypatch, tmp_path):
    """startup 钩子必须真的把上传目录建出来（原来的测试根本没跑到 lifespan）。

    为什么单独测这个？
        上传目录是第一个接口就要用的东西。建不出来时最坏的情况是：
        启动日志看着一切正常，直到有人上传文件才报 FileNotFoundError，
        而那时你正在给别人演示。

    做法：把 UPLOAD_DIR 指向一个还不存在的新目录（tmp_path 只是拿来拼路径，
    真正建目录的是 lifespan 自己），然后用 `with TestClient(app)` 触发 lifespan。

    注意 config.reset_settings_cache() 这行**不能省**，它也是刻意的：
    上面的 test_settings_cache_can_actually_be_reset 已经清过一次缓存，
    所以这条测试实际上依赖"测试执行顺序"才能抓住漏改的现取写法 ——
    单独跑它时，旧写法也能过（因为缓存是冷的）。这类隐性依赖越少越好，
    所以这里显式清缓存，让它在任何顺序下都测同一件事。
    """
    target = tmp_path / "brand-new-uploads"
    assert not target.exists()  # 前提：先确认它真的不存在，否则这条测试没意义

    monkeypatch.setenv("UPLOAD_DIR", str(target))
    config.reset_settings_cache()
    try:
        # 这里**必须**用 with：TestClient 只有在上下文管理器里才会跑 lifespan。
        with TestClient(app) as started:
            body = started.get("/api/health").json()

        assert body["data"]["upload_dir_ready"] is True
        assert target.is_dir()  # 接口说 True 不够，要落到文件系统上确认
    finally:
        config.reset_settings_cache()


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
