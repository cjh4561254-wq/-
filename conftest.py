"""pytest 的全局装配文件 —— 在**任何测试收集之前**执行。

这个文件目前只做一件看起来很小、但很重要的事：**把临时目录从项目根目录挪走。**


为什么需要它（这是实测出来的真问题，不是预防性措施）
--------------------------------------------------

本项目所在机器的 Python 子进程**无法写入系统临时目录**。实测：

    >>> import tempfile, os
    >>> os.environ["TEMP"]
    'C:\\Users\\jun\\AppData\\Local\\Temp'
    >>> fd = os.open(os.path.join(os.environ["TEMP"], "probe_x"), os.O_CREAT | os.O_EXCL)
    PermissionError: [Errno 13] Permission denied: '...\\Temp\\probe_x'

于是 `tempfile` 会依次试它认为可用的候选目录，全部失败后**退回当前工作目录**：

    FileNotFoundError: [Errno 2] No usable temporary directory found in
      ['C:\\Users\\jun\\AppData\\Local\\Temp', ..., 'C:\\Windows\\Temp',
       'c:\\temp', 'c:\\tmp', '\\temp', '\\tmp', 'C:\\Windows']

而 `tempfile.gettempdir()` 的兜底逻辑就是"用 cwd"（见 CPython Lib/tempfile.py
的 `_get_default_tempdir`）。在本项目里，cwd = 项目根目录，后果是：

1. 跑一次测试，仓库根目录就多出 `pytest-of-jun/`（pytest 的 basetemp）和
   若干 `tmpXXXXXXXX/`（各测试里 `tmp_path`／`tempfile.mkdtemp()` 的产物）。
2. 更危险的是：**只要有人手滑执行一次 `git add -A`，这些垃圾就进仓库了** ——
   而"手滑 `git add -A`"正是本项目 `docs/DECISIONS.md` D-05 记录的那次真实事故。
3. 它还会掩盖问题：`git status` 看着"脏"，但脏的并不是谁的代码。

这里把三个环境变量**显式指到项目内的 `.tmp/`**（已加入 .gitignore），
`tempfile.tempdir` 缓存也一并清掉，因为：

- `tempfile.tempdir` 一旦被赋值（哪怕赋成 `None` 之外的任何东西）就会被缓存，
  不清掉的话本文件设的环境变量不生效；
- 设成项目内的目录是**刻意的**：这样"测试产物写到哪儿"是可见、可控、可清理的，
  而不是依赖某台机器的系统配置。换一台机器（CI 容器、别人的笔记本）行为一致。


为什么是 `conftest.py` 而不是别的地方
------------------------------------

pytest 保证 rootdir 下的 `conftest.py` 在任何测试模块被导入之前加载，
所以放在这里，`tests/` 里所有 `tmp_path` 以及被测代码里的 `tempfile` 调用
都会走 `.tmp/`。放在 `app/config.py` 里则太早（`pytest` 自己收集阶段的
`tmp_path` 还没用到）也太隐晦（那是业务配置，不该管测试产物）。


已知边界：这里只覆盖 pytest，覆盖不了别的入口
--------------------------------------------

`python -m uvicorn app.main:app` 或手工脚本在项目根启动时，`gettempdir()`
仍然会指向项目根 —— 因为改环境变量的是本文件，而它们不加载本文件。

目前这不构成实际风险：全仓库只有本文件直接调用 `tempfile`；matplotlib 的
缓存由 `app/config.py` 里的 `MPLCONFIGDIR` 单独兜住。但这个"同源问题的
另一半没被覆盖"是知道的，已记录在 `docs/DECISIONS.md`（D-07）。
真要修，得把改环境变量挪到 `app/__init__.py`，代价是"import 库就改全局
环境"这种副作用更脏 —— 不值得，所以选择记录而不是修。
"""

from __future__ import annotations

import os
import tempfile
import warnings
from pathlib import Path

import pytest

# 项目根目录 = 本文件所在目录（tests/ 与 app/ 的同级）
PROJECT_ROOT = Path(__file__).resolve().parent

# 测试产物的统一落点。用点号开头：一眼能看出是"运行时生成、不属于源码"。
# 必须在 .gitignore 里（已加），否则等于把垃圾从根目录挪到子目录而已。
TEMP_DIR = PROJECT_ROOT / ".tmp"

# ── 第 1 道保险：把 tempfile 的三个环境变量指到 .tmp/ ──────────────────────
# 这一步**不能让它把整个测试会话搞崩**。曾经的真实后果：有人把 .tmp 建成了一个
# 同名文件，于是 mkdir 抛 FileExistsError，报错停在 conftest 里、pytest 直接
# 起不来（ImportError while loading conftest），看起来像"测试全坏了"，实际
# 跟被测代码毫无关系。所以这里降级为 warning + 继续跑：脏目录是可接受的，
# 起不来测试是不可接受的。
_TEMP_ENV_SETUP_OK = True
try:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:  # 权限不足、同名文件挡路等
    _TEMP_ENV_SETUP_OK = False
    warnings.warn(
        f"无法创建测试临时目录 {TEMP_DIR}（{type(exc).__name__}: {exc}）。"
        "已跳过 tempfile 重定向，本次会话的临时文件可能落到项目根目录"
        f"（.gitignore 里有 /tmp????????/ 与 pytest-of-*/ 兜底）。"
        "测试会照常运行，但这台机器上的临时产物不受控。",
        stacklevel=1,
    )

if _TEMP_ENV_SETUP_OK:
    # 让 tempfile 模块以及一切读这三个变量的第三方库（matplotlib、openpyxl 等）
    # 都把临时文件写到这里。
    os.environ["TMPDIR"] = str(TEMP_DIR)
    os.environ["TEMP"] = str(TEMP_DIR)
    os.environ["TMP"] = str(TEMP_DIR)

    # 清掉缓存，否则上面三行不生效（tempfile 会把第一次算出的结果缓存起来）。
    tempfile.tempdir = None


def pytest_configure(config: pytest.Config) -> None:
    """第 2 道保险：直接钉住 pytest 自己的 basetemp。

    为什么环境变量还不够：`tmp_path` / `tmp_path_factory` 的落点由 pytest 的
    `basetemp` 决定，而 `basetemp` 的默认值是 `tempfile.gettempdir()/pytest-of-<user>`。
    只要那条链路上任何一环变了（有人在命令行传 --basetemp、tempfile 的缓存被
    别的插件改过、我们上面的 mkdir 失败降级了），产物就会跑回项目根目录。
    显式指定相对路径 `.tmp/pytest` 后，pytest 会相对 rootdir 解析它并自己创建，
    与上面成功与否都无关 —— 两道保险叠加，产物的落点是确定的。
    """
    if not config.option.basetemp:
        config.option.basetemp = ".tmp/pytest"
