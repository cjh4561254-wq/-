"""应用配置：把**系统环境变量**里的文本变成有类型、可校验的 Python 对象。

两件事在这一版里变了（见 docs/DECISIONS.md D-19）：

1. **不再读 `.env` 文件**。配置来源只剩一个：进程的环境变量（Windows 上就是
   「系统属性 → 环境变量」里那些）。`.env.example` 保留下来只当"有哪些键"的清单用，
   它已经不再是模板。
2. **大模型三项各认两个环境变量名**：`LLM_*` 优先，`DEEPSEEK_*` 兜底。
   本机环境里只有 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` 这套 DeepSeek 官方
   名字，而应用此前只认 `LLM_*` —— 于是"机器上明明配了 key，应用却报没配置"。

实测过（`.tmp/probe_env_compare.py`）：旧 `.env` 里的 15 个键，值**全部**等于
Settings 的默认值。所以"不再读 .env"对当前取值零影响，是纯粹的减法 ——
这一点很重要，否则就变成一边改配置源一边悄悄改行为。
"""

from functools import lru_cache
import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# matplotlib 的缓存目录。**这个必须留在 import 时刻**，理由和临时目录不同：
# matplotlib 在 import 时就决定配置目录（读 MPLCONFIGDIR），等 lifespan 已经晚了。
# 这里不猜任何东西：目录是我们自己创建的项目内目录，mkdir + setdefault 即可。
# 本机 C:\Users\jun\AppData\Local\matplotlib 拒绝访问（WinError 5），
# 不设置的话 matplotlib 会退化成"每次导入都新建临时缓存目录"，又慢又会扔垃圾。
_MPL_CACHE_DIR = PROJECT_ROOT / ".mplcache"
_MPL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE_DIR))

# 临时目录（TEMP/TMP/TMPDIR）**故意不在这里设置**，改由 app/runtime.py 的
# runtime_environment() 在服务启动时显式设置。原因：
#   1. import 时刻猜"系统临时目录能不能写"踩过坑（设了但写不进 → tempfile 静默
#      退回 cwd，上传的临时文件掉进仓库根，且没有任何报错）；
#   2. 放在 lifespan 里，行为可以被测试观察、可以按配置覆盖（TEMP_DIR）。
# 两者机制不同是因为约束不同，不是疏忽。

# 大模型三个字段各自认的环境变量名，**按优先级从高到低**（AliasChoices 取第一个
# 存在的）。字段声明与测试夹具共用这三个常量，避免"实现换了名字、测试还在清旧名字"
# 的漂移：少清一个，本机真实密钥就会渗进"未配置"用例 —— 于是测试在没 key 的机器上
# 绿、在有 key 的机器上红，而后者才是用户的机器。
_LLM_BASE_URL_NAMES = ("LLM_BASE_URL", "DEEPSEEK_BASE_URL")
_LLM_MODEL_NAMES = ("LLM_MODEL", "DEEPSEEK_MODEL")
_LLM_API_KEY_NAMES = ("LLM_API_KEY", "DEEPSEEK_API_KEY")

#: 上面三组名字的并集。测试用它把环境清干净（见根 conftest.py 的隔离夹具）。
LLM_ENV_VAR_NAMES: tuple[str, ...] = (
    *_LLM_BASE_URL_NAMES,
    *_LLM_MODEL_NAMES,
    *_LLM_API_KEY_NAMES,
)


class Settings(BaseSettings):
    """配置只负责取值；创建目录和设置运行环境由 main.py 的 lifespan 负责。"""

    model_config = SettingsConfigDict(
        # **故意没有 env_file**：不读 .env（理由见模块 docstring）。
        # 想临时读某个 dotenv 文件，仍然可以显式传 Settings(_env_file=...)。
        case_sensitive=False,
        extra="ignore",
        # validation_alias 会把"字段名"从取值来源里顶掉，而 Settings(llm_api_key="x")
        # 这种按字段名传参在测试里用得很顺手，所以显式允许（pydantic ≥2.11 的写法，
        # 老写法叫 populate_by_name；用新的，免得踩弃用告警）。
        validate_by_name=True,
    )

    app_name: str = "AI Data Analyst Agent"
    app_version: str = "0.1.0"

    # 相对路径都相对于项目根，不随启动命令所在目录改变。
    upload_dir: str = "data/uploads"
    temp_dir: str = Field(default=".tmp", min_length=1)
    max_upload_mb: int = Field(default=10, gt=0, le=200)
    max_rows: int = Field(default=200_000, gt=0, le=5_000_000)
    max_columns: int = Field(default=200, gt=0, le=2_000)

    # 第四阶段才使用；当前不会发出任何模型请求。
    #
    # 名字顺序（LLM_* 在前、DEEPSEEK_* 在后）不能反：反了的话，本机那个
    # DEEPSEEK_* 会悄悄盖掉显式设置的 LLM_*，而"我明明设了 LLM_MODEL 却不生效"
    # 是最难查的一类配置问题。
    llm_base_url: str = Field(default="", validation_alias=AliasChoices(*_LLM_BASE_URL_NAMES))
    # 模型名有默认值，因为本机环境只给了 base_url 与 key、没有模型名这一项；
    # 留空会让 llm_configured 永远为 False —— 表现就是"配了 key 却不生效"。
    # 取值依据：实测 GET https://api.deepseek.com/models 返回
    # `deepseek-flash` 与 `deepseek-v4-pro` 两个（前者是对话模型）。
    # 要换服务/换模型：设 LLM_MODEL（或 DEEPSEEK_MODEL）。
    llm_model: str = Field(default="deepseek-flash", validation_alias=AliasChoices(*_LLM_MODEL_NAMES))
    llm_api_key: str = Field(default="", repr=False, validation_alias=AliasChoices(*_LLM_API_KEY_NAMES))
    llm_timeout_seconds: int = Field(default=60, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    # 第五阶段（图表）才写入文件，但字段现在就加：架构文档 §4.0.1 给这张表定过一条
    # 纪律 —— "写配置相关代码前先在表里查一下，不要自己造名字相近的字段"，
    # 造出来还不报错，只是永远不生效。这里一次把 D4/D5 要用的三个补齐。
    charts_dir: str = Field(default="data/charts", min_length=1)

    # 整条工作流的总时长上限（秒）。轮询接口按 heartbeat_at 惰性判定超时。
    max_workflow_seconds: int = Field(default=180, gt=0, le=3600)

    # 一条工作流里所有工具调用次数的总上限。防"模型重试 + 任务重试"组合成死循环。
    tool_call_limit: int = Field(default=20, ge=1, le=200)

    @property
    def upload_path(self) -> Path:
        """上传目录的绝对路径。"""
        p = Path(self.upload_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def charts_path(self) -> Path:
        """图表目录的绝对路径（`charts_dir` 是配置字符串，这个是可直接用的 Path）。"""
        p = Path(self.charts_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def temp_path(self) -> Path:
        """应用临时目录的绝对路径。

        注意区分：`temp_dir` 是**配置字符串**（可能写的是相对路径），
        `temp_path` 才是可以直接 `mkdir` / 传给 `tempfile` 的绝对路径。
        上传目录的 `upload_dir` / `upload_path` 是同一组关系。

        这个目录不是"上传文件的长期存储目录"——上传的原始字节在
        `settings.upload_path` 里，这里放的是**用完就删的中间文件**
        （例如 Starlette 处理大上传时的 spool 文件）。原因见 D-07。
        """
        p = Path(self.temp_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        """仅说明三项配置非空，不代表真实 API 调用成功。

        `llm_model` 现在有默认值，所以实际起作用的是 base_url 与 api_key 两项；
        字段本身仍然是三项都要非空 —— 空 base_url 表示"明确不接模型"。
        """
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """业务函数中调用 config.get_settings()，不要保存模块级 settings 快照。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试修改配置后清缓存，下一次 get_settings() 会重新读取。"""
    get_settings.cache_clear()
