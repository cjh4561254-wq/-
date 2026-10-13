"""应用配置：把 .env 里的文本变成有类型、可校验的 Python 对象。

为什么单独放一个文件？
    配置是全局共享的，散落在各处会让"这个值从哪来"变得不可追踪。
    集中在这里以后，想知道任何配置的含义只需要看这一个文件。
"""

from functools import lru_cache
import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：本文件在 <root>/app/config.py，所以往上两级就是 <root>
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 让 matplotlib 把缓存写进项目目录。
# 原因：本机 C:\Users\jun\AppData\Local\matplotlib 拒绝访问（WinError 5），
# matplotlib 会退化成"每次导入都新建临时缓存目录"，既慢又会往项目根目录扔垃圾。
# 必须在 import matplotlib 之前设置，所以放在这个最早被导入的文件顶部。
_MPL_CACHE_DIR = PROJECT_ROOT / ".mplcache"
_MPL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE_DIR))


class Settings(BaseSettings):
    """全部配置项。字段名会去环境变量 / .env 里找同名（不区分大小写）的值。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # .env 里有多余的键不报错，避免误伤
    )

    # ---- 应用 ----
    app_name: str = "AI Data Analyst Agent"
    app_version: str = "0.1.0"

    # ---- 上传 ----
    upload_dir: str = "data/uploads"
    max_upload_mb: int = Field(default=10, gt=0, le=200)

    # ---- 大模型（第四阶段才用到）----
    llm_base_url: str = ""
    llm_model: str = ""
    # repr=False：任何地方 print(settings) 或写日志都不会带出密钥原文
    llm_api_key: str = Field(default="", repr=False)
    llm_timeout_seconds: int = Field(default=60, gt=0, le=300)
    llm_max_retries: int = Field(default=2, ge=0, le=5)

    @property
    def upload_path(self) -> Path:
        """上传目录的绝对路径。相对路径按项目根目录解释。"""
        p = Path(self.upload_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def max_upload_bytes(self) -> int:
        """大小上限换算成字节，方便直接和文件长度比较。"""
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_configured(self) -> bool:
        """三个关键项都填了才算配置完整。只填一半会在调用时报错，不如提前判断。"""
        return bool(self.llm_base_url and self.llm_model and self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回全局唯一的配置对象。

    为什么要包成函数、而不是直接写 `settings = Settings()`？
        因为模块级对象在 import 时就固定了。测试里想换一个配置（比如把
        max_upload_mb 改成 1 来测超限），改环境变量后 `cache_clear()` 并不
        影响别人早就 import 走的那份引用 —— 测试会"静默失效"，看着绿其实
        没测到东西。包成函数后，业务代码每次现取，测试才有真正的控制权。

    使用约定（重要）：
        业务模块请 `from app import config`，然后在函数内部调用
        `config.get_settings()`。
        不要写 `from app.config import settings` —— 那样又会在 import 时
        绑定死，回到上面那个坑里。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清空配置缓存。测试里改完环境变量后调用它。"""
    get_settings.cache_clear()

