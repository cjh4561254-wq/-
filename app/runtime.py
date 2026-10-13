"""进程运行状态，以及服务启动时显式准备的运行环境。"""

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
from collections.abc import Iterator

from app.config import PROJECT_ROOT, Settings

STARTED_AT: float = time.time()


def uptime_seconds() -> float:
    """服务已运行的秒数。"""
    return round(time.time() - STARTED_AT, 2)


@contextmanager
def runtime_environment(settings: Settings) -> Iterator[Path]:
    """启动时创建临时目录并让 tempfile 用它；退出时恢复进程原有配置。

    输入：Settings。输出：yield 临时目录的绝对路径。

    为什么临时目录放在这里、而不是像 MPLCONFIGDIR 那样放在 import 时刻
    （原因不同，见 app/config.py 的注释）：
        - tempfile 直到第一次调用 gettempdir() 才决定用哪个目录，所以"在服务
          真正开始处理请求之前设好"就来得及，还能被测试观察、按配置覆盖；
        - 而 import 时刻去猜"系统临时目录能不能写"已经踩过坑：设进去的路径写不进
          时，tempfile 会静默退回当前工作目录，上传的临时文件掉进仓库根，
          且没有任何报错。

    `TEMP_DIR` 是应用**明确指定**的目录（默认项目内 `.tmp`），不继承、不猜测
    系统 `TEMP`：这样同一份代码在哪台机器上行为一致，产物也留在项目里可清理。
    """
    temp_path = settings.temp_path.resolve()
    temp_path.mkdir(parents=True, exist_ok=True)
    # 真写一个文件，而不是只看目录在不在：写不进去就让启动失败，保留原始异常。
    with tempfile.TemporaryFile(dir=temp_path):
        pass

    names = ("TMPDIR", "TEMP", "TMP")
    previous = {name: os.environ.get(name) for name in names}
    previous_tempdir = tempfile.tempdir
    try:
        for name in names:
            os.environ[name] = str(temp_path)
        # tempfile 的答案有缓存：不改它，上面三行不生效（gettempdir 只在缓存为空时
        # 才重新探测）。赋 None 是它官方支持的"下次重新探测"写法。
        tempfile.tempdir = None
        yield temp_path
    finally:
        # 恢复现场：退出启动上下文（例如测试里用 TestClient 进出几次）之后，
        # 进程里不能留下我们改过的环境变量或缓存。
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        tempfile.tempdir = previous_tempdir
