"""配置层的取值规则：认哪些环境变量名、谁优先、以及**不再读 `.env`**。

为什么这个文件值得单独写
------------------------
配置写错了不会报错，只会**静默不生效**：机器上明明有 key，应用却说自己没配置，
于是整条工作流悄悄退回规则兜底 —— 接口正常、日志正常、模型一次都没被调用过。
这类问题没有异常也没有堆栈，只有把"取值规则"本身钉成测试才抓得住。

本文件只构造 `Settings`、只碰环境变量，不启动服务、不发任何网络请求。
"""

from __future__ import annotations

import pytest

from app import config

# 与用户机器上那两个机器级环境变量同名的值（密钥是假的，只在本进程里存在）。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
SENTINEL_KEY = "sk-sentinel-not-a-real-key-000000000000"


@pytest.fixture
def settings_factory(monkeypatch):
    """返回一个构造器：先清掉六个名字，再按需 `setenv`，最后构造 `Settings()`。

    清 + 设的顺序不能反：反了就成了"本机有什么就是什么"，用例结果取决于机器。
    """

    def _build(**env: str) -> config.Settings:
        for name in config.LLM_ENV_VAR_NAMES:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        config.reset_settings_cache()
        return config.Settings()

    yield _build
    config.reset_settings_cache()


# ---------------- 名字清单本身 ----------------


def test_the_documented_names_are_exactly_these_six():
    """清单在这里是**字面量**，不是从实现里读出来的。

    这样改名字就必须同时改测试（以及文档），不会出现"实现加了新名字、隔离夹具
    还在清旧名字"的漂移 —— 漏掉的那个名字恰好就是本机真实存在的那个。
    """
    assert set(config.LLM_ENV_VAR_NAMES) == {
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_API_KEY",
    }


@pytest.mark.parametrize(
    ("env_name", "field_name"),
    [
        ("LLM_BASE_URL", "llm_base_url"),
        ("DEEPSEEK_BASE_URL", "llm_base_url"),
        ("LLM_MODEL", "llm_model"),
        ("DEEPSEEK_MODEL", "llm_model"),
        ("LLM_API_KEY", "llm_api_key"),
        ("DEEPSEEK_API_KEY", "llm_api_key"),
    ],
)
def test_every_documented_name_reaches_its_field(settings_factory, env_name: str, field_name: str):
    """清单里的六个名字，每一个都要真的接上线 —— 只列在清单里等于没写。"""
    probe = "probe-value"
    assert getattr(settings_factory(**{env_name: probe}), field_name) == probe


# ---------------- 本机那套 DeepSeek 官方名字 ----------------


def test_deepseek_names_alone_are_enough_to_be_configured(settings_factory):
    """用户机器上只有 `DEEPSEEK_*`；这两项在，就必须算"配好了"。

    这就是本文件存在的理由：改之前应用只认 `LLM_*`，于是"机器上明明有 key，
    应用却报没配置" —— 而且不会报错，只会永远走规则兜底。
    """
    s = settings_factory(DEEPSEEK_BASE_URL=DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY=SENTINEL_KEY)

    assert s.llm_configured is True
    assert s.llm_base_url == DEEPSEEK_BASE_URL
    assert s.llm_model == "deepseek-flash"  # 本机没有模型名这一项 → 落到默认值


def test_llm_names_win_when_both_families_are_set(settings_factory):
    """两套名字都给时 `LLM_*` 优先。

    反过来的话，本机那个 `DEEPSEEK_*` 会悄悄盖掉显式设置的 `LLM_*`，
    表现为"我明明设了 LLM_MODEL 却不生效" —— 最难查的一类配置问题。
    """
    s = settings_factory(
        LLM_BASE_URL="https://llm.example/v1",
        DEEPSEEK_BASE_URL=DEEPSEEK_BASE_URL,
        LLM_API_KEY="llm-key",
        DEEPSEEK_API_KEY=SENTINEL_KEY,
    )

    assert s.llm_base_url == "https://llm.example/v1"
    assert s.llm_api_key == "llm-key"


def test_deepseek_model_name_overrides_the_default(settings_factory):
    """模型名有默认值，但默认值必须**可以被覆盖**（DeepSeek 另一个模型是 v4-pro）。"""
    assert settings_factory(DEEPSEEK_MODEL="deepseek-v4-pro").llm_model == "deepseek-v4-pro"


def test_an_empty_llm_name_shadows_the_deepseek_fallback(settings_factory):
    """显式设成空串要能压住兜底名字。

    没有这条，用户想"临时关掉模型"（而不是去删机器级密钥）就做不到 ——
    设 `LLM_API_KEY=""` 会被兜底名字接住，然后拿旧密钥继续发请求。
    """
    s = settings_factory(
        LLM_API_KEY="",
        DEEPSEEK_BASE_URL=DEEPSEEK_BASE_URL,
        DEEPSEEK_API_KEY=SENTINEL_KEY,
    )

    assert s.llm_api_key == ""
    assert s.llm_configured is False


def test_the_key_is_never_in_repr_even_when_it_comes_from_the_deepseek_name(settings_factory):
    """安全底线：换名字进来也不能让 `print(settings)` 泄漏密钥。"""
    s = settings_factory(DEEPSEEK_BASE_URL=DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY=SENTINEL_KEY)

    assert s.llm_api_key == SENTINEL_KEY  # 程序内部拿得到
    assert SENTINEL_KEY not in repr(s)  # 打印时看不到


# ---------------- 不再读 .env ----------------


def test_a_dotenv_file_next_to_the_process_is_ignored(monkeypatch, tmp_path):
    """工作目录里放一个 `.env` 也不能影响取值：配置来源只有一个 —— 环境变量。

    用 `tmp_path` 而不是仓库根，是为了不碰开发者自己那个 `.env`；
    再把工作目录切过去，这样"到底读没读 dotenv"才有区分度。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MAX_ROWS=424242\nLLM_API_KEY=sentinel\n",
        encoding="utf-8",
        newline="",  # D-16 第 4 条：不写这个，Windows 会把 \n 变成 \r\n
    )

    s = config.Settings()

    assert s.max_rows == 200_000  # 默认值，不是 .env 里的 424242
    assert s.llm_api_key == ""


def test_env_file_is_not_declared_at_all():
    """把"不读 .env"钉在配置声明上：谁把 `env_file` 加回来，这条就红。

    判据是**键的值**，不是"键在不在"：`SettingsConfigDict` 本来就带一个
    `env_file=None` 的默认键（pydantic-settings 2.15.0 实测：
    `"env_file" in Settings.model_config` 为 True、值为 None）。写成
    `"env_file" not in model_config` 会在什么都没改的情况下就红 ——
    一条永远红的用例抓不住任何回归，只会被人学会忽略。

    同时确认"显式传 `_env_file=` 仍然可用"没被顺手带走：实测
    `Settings(_env_file=<临时文件>)` 里写的 `MAX_ROWS=424242` 照样生效。
    """
    assert config.Settings.model_config.get("env_file") is None
