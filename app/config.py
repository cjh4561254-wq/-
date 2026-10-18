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

    # 历史归档库（D6 追加）。**空字符串 = 明确关闭归档**：工作流照常跑，只是不落库，
    # 所以这个字段**故意不加** min_length（与 temp_dir / charts_dir 那几个不同）。
    #
    # 选址是 `data/db/app.db`，而不是 `docs/ARCHITECTURE.md` 第 14 节决策 3 附近写的
    # `data/app.db` —— 那是设计阶段随手起的名字。落地时按**仓库已有的 ignore 规则**
    # 选址：`.gitignore` 早就挡了 `data/db/*`（sqlite 写事务期间还会生成
    # `app.db-journal`，只挡 `*.db` 是挡不住它的），而 `data/app.db` 得再往
    # .gitignore 里加一条新规则。少一条规则就少一处将来会忘的地方。
    #
    # 环境变量名就是一个：`DB_PATH`（不需要 AliasChoices —— 那是给 LLM 三项
    # "历史名字 + 新名字"用的，归档库没有历史包袱）。
    db_path: str = "data/db/app.db"

    # ---------------- 账号与会话（真实认证） ----------------
    #
    # 为什么是**另一个**库文件而不是复用 `db_path`：
    # `DB_PATH` 为空串 = 明确关闭归档。而认证不可关闭（关掉就没有登录了）。
    # 放同一个文件会出现"为了关归档而清空 DB_PATH，结果连账号库一起挪走"的连带伤害。
    # 两个库各自独立，谁也关不掉谁。`.gitignore` 里 `data/db/*` 已经覆盖了
    # `auth.db` 与写事务期间的 `auth.db-journal`。
    auth_db_path: str = "data/db/auth.db"

    # 会话 cookie 的名字。带项目前缀而不是通用的 `session`：
    # 本机 127.0.0.1 上常同时跑着好几个项目（本机就有 8000 端口的另一个项目），
    # 通用的 `session` 会和别人的 cookie 撞名，互相覆盖。
    auth_cookie_name: str = "mis_session"

    # `Secure` 标志。**本机开发必须是 False** —— 服务跑在 http://127.0.0.1，
    # 带 Secure 的 cookie 浏览器根本不会回传，表现是"登录成功但立刻又变未登录"。
    # 部署到 HTTPS 时**必须**设成 True（`AUTH_COOKIE_SECURE=1`），
    # 否则会话 token 会以明文走在网络上。
    auth_cookie_secure: bool = False

    # 会话有效期。默认 8 小时 = 一个工作日，不是 30 天：
    # 这是一个能看全部业务数据的后台，会话过期时间越长，token 被拿走的窗口越大。
    auth_session_seconds: int = Field(default=8 * 3600, gt=60, le=30 * 24 * 3600)

    # PBKDF2-HMAC-SHA256 的迭代次数。
    # 240000 是**刻意选慢**的：单次约 0.1 秒，用户登录时感觉不到，
    # 但让离线暴力破解的成本高了几个数量级。
    # 这个值会**随每个账号存进库**，所以以后调高它不会让老账号登不进来。
    auth_pbkdf2_iterations: int = Field(default=240_000, ge=1000, le=10_000_000)

    # 登录失败限流：连续失败 auth_max_failed_attempts 次后锁 auth_lockout_seconds 秒。
    # 这是**按用户名**计的（内存里），不是按 IP —— 本机场景下按 IP 没有意义。
    auth_max_failed_attempts: int = Field(default=5, ge=2, le=100)
    auth_lockout_seconds: int = Field(default=300, gt=0, le=24 * 3600)

    @property
    def auth_db_file(self) -> Path:
        """账号库文件的绝对路径。

        与 `db_file` 同一个坑：`Path("")` 是 `.`，空串会返回**项目根目录**。
        调用方必须先判断认证是否开启 —— 便捷入口 `app.data.users.get_auth_store()`
        已经把这一层处理掉了（`auth_db_path` 为空时返回 `None`）。
        """
        p = Path(self.auth_db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p

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
    def db_file(self) -> Path:
        """归档库文件的绝对路径（`db_path` 是配置字符串，这个是可直接用的 Path）。

        注意**空字符串**这种情形：`Path("")` 是 `.`，于是这里会返回**项目根目录**。
        别拿这个结果直接去 `sqlite3.connect()` —— 调用方要先判断归档是否开启；
        便捷入口 `app.data.history.get_history_store()` 已经把这一层处理掉了
        （`db_path` 为空时它返回 `None`）。
        """
        p = Path(self.db_path)
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


def current_settings() -> Settings:
    """在**调用时刻**取当前配置。

    与直接 `get_settings()` 的差别只有一个，但很关键：这个函数通过模块全局
    去取 `get_settings`，所以 `monkeypatch.setattr(config, "get_settings", fake)`
    能生效；而 `from app.config import get_settings` 会在导入时就把**那个函数对象**
    绑死，之后谁替换 `config.get_settings` 都影响不到它。

    为什么需要它：`app/data/users.py` 的 `get_auth_store()` 必须读**当前**配置
    （测试用 AUTH_DB_PATH 做隔离，每个用例一个临时账号库）。如果它绑死导入期的
    `get_settings`，测试里"换了一个空库"会被无视 —— 实测表现就是
    `test_bootstrap_creates_first_admin_and_logs_in` 拿到 403
    "账号库已经有账号了"（它其实查的是上一个用例的库），
    以及 `get_auth_store()` 在 `auth_db_path=""` 时仍然返回一个 store。

    为什么不在 users.py 里写 `config.get_settings()`：那样每个数据层模块都要
    多一个 `import app.config` 并记住"要动态取"。放在配置模块里只有一处。
    """
    return get_settings()
