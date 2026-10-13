"""应用配置：把环境变量 / .env 里的文本变成有类型、可校验的 Python 对象。"""

from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field
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


class Settings(BaseSettings):
    """配置只负责取值；创建目录和设置运行环境由 main.py 的 lifespan 负责。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_timeout_seconds: int = Field(default=60, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    @property
    def upload_path(self) -> Path:
        """上传目录的绝对路径。"""
        p = Path(self.upload_dir)
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
        """仅说明三项配置非空，不代表真实 API 调用成功。"""
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """业务函数中调用 config.get_settings()，不要保存模块级 settings 快照。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试修改配置后清缓存，下一次 get_settings() 会重新读取。"""
    get_settings.cache_clear()
