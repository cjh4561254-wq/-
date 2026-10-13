"""pytest 的全局装配文件 —— 在**任何测试收集之前**执行。

这个文件做两件事，都是"看起来很小、但很重要"：

1. **把临时目录从项目根目录挪走**（第 1 道保险：环境变量）。
2. **钉住 pytest 自己的 `basetemp`**，并从子目录跑时把 `app/` 补回收集范围
   （`pytest_configure` 钩子）。


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
都会走 `.tmp/`。

"覆盖不了别的入口"这个洞**已经补上了**（2026-10-13，但换了一种机制）：
临时目录现在由 `app/runtime.py` 的 `runtime_environment()` 在**服务启动时**设置
（`app/main.py` 的 lifespan 包住 `yield`），不再靠"import 时改环境变量"。

分工要说清楚，否则很容易看成"同一件事写了两遍"：

- `app/runtime.py`（生产路径）：服务启动时把 `TEMP`/`TMP`/`TMPDIR` 指到
  `settings.temp_path`（默认项目内 `.tmp/`，可用 `TEMP_DIR` 覆盖），退出时恢复现场。
  `python -m uvicorn app.main:app` 起来的服务走的是这条。
- `conftest.py`（本文件，测试路径）：在**收集测试之前**就把三个变量设好。
  必须这么早，因为 pytest 自己的 `basetemp` 默认值就来自
  `tempfile.gettempdir()`，而它比任何 lifespan 都早。
- `app/config.py` **不再**设置临时目录，只保留 `MPLCONFIGDIR` ——
  因为 matplotlib 在 import 时就决定配置目录，来不及等到启动。

为什么早期版本"import 时设置"被推翻（两条，第二条才是关键）：
1. import 一个包就改进程全局环境，副作用太脏，而且"谁先 import 了谁"决定行为；
2. 它**猜错过**：看到 `TEMP` 已设就尊重它，而这台机器上 `TEMP` 指向的目录
   存在却写不进去，于是临时文件照旧掉回仓库根，全程没有任何报错。
现在改成"应用明确指定自己的临时目录"，不继承、不猜测系统配置。

上面那段"已知边界"的原话保留在 `docs/DECISIONS.md`（D-07）作为当时的判断记录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

# 项目根目录 = 本文件所在目录（tests/ 与 app/ 的同级）
PROJECT_ROOT = Path(__file__).resolve().parent

# 把项目根放进 sys.path，让 `import app` 与 `import tests` 与"从哪儿跑 pytest"无关。
#
# 为什么需要：pytest.ini 里的 `pythonpath = .` 是相对 rootdir 生效的，但实测
# 从 `tests/` 子目录跑时 `app` 包**收不到**（`--collect-only` 里没有 `<Package app>`，
# 那条 `app/errors.py` 的 doctest 直接消失：根目录 88 passed、子目录 87 passed）。
# 也就是说那条 doctest 回归网此前只是**碰巧**在根目录能跑（rootdir 恰好被插进
# sys.path），一旦有人在别的入口跑就静默少一条 —— 静默少一条比明着报错更危险。
# 这里显式插一次，行为就与入口无关了。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
    """两个钩子动作合并在这里。**这里必须只有一个 `pytest_configure`。**

    ⚠️ 合并原因（真实事故，别改回去）：这个函数原来被定义了两次 ——
    "钉 basetemp" 与 "补收集 app/" 各一个。Python 里同名函数**后者覆盖前者**，
    所以第一个**从来没有执行过**。它的危害不是功能坏了（basetemp 走默认值
    `tempfile.gettempdir()/pytest-of-<user>`，而上一段已经把 `TEMP` 指到
    `.tmp/`，所以恰好也落在 `.tmp/` 里，仓库根是干净的），
    而是**注释谎报**：那一大段"两条踩过的坑"描述的是不会执行的代码。
    同时 `--basetemp` 尊重逻辑、mkdir 降级逻辑**全都不生效**。
    发现方式：backend-engineer 观察 `.tmp/pytest` 的 mtime 在跑测试前后没变，
    而 `.tmp/pytest-of-jun` 每次都在变。

    ---------------------------- 动作 1 ----------------------------
    钉住 pytest 自己的 basetemp。

    为什么环境变量还不够：`tmp_path` / `tmp_path_factory` 的落点由 pytest 的
    `basetemp` 决定，而 `basetemp` 的默认值是 `tempfile.gettempdir()/pytest-of-<user>`。
    只要那条链路上任何一环变了（有人在命令行传 --basetemp、tempfile 的缓存被
    别的插件改过、我们上面的 mkdir 失败降级了），产物就会跑回项目根目录。
    显式指定后，产物的落点是确定的 —— 与上面成功与否都无关。

    两条踩过的坑，都写在这里免得以后改回去：

    1) **必须用绝对路径，不能写相对的 ".tmp/pytest"。** pytest 会把相对的
       basetemp 按**当前工作目录**解析（不是 rootdir）：`cd tests` 再跑就变成
       `<root>/tests/.tmp/pytest`，而 conftest 只建了 `<root>/.tmp`。
    2) **`.tmp/` 必须已经存在。** pytest 用 `basetemp.mkdir(mode=0o700)` 创建它，
       没有 `parents=True`、也没有 `exist_ok=True`（见 `_pytest/tmpdir.py`
       的 `TempPathFactory.getbasetemp`），所以父目录不存在会抛
       `FileNotFoundError: [WinError 3]`，`.tmp` 是个同名文件则抛
       `NotADirectoryError`/`FileExistsError`。

    这两条合起来曾经造成一个真实回归：从子目录跑时**唯一用 `tmp_path` 的那个测试
    在 setup 阶段就 ERROR**，看起来跟被测代码毫无关系。所以下面用 conftest 自己的
    绝对路径（`TEMP_DIR` 由 `Path(__file__).resolve().parent` 推出，与 CWD 无关），
    并且**只在 `.tmp/` 确认可用时才钉** —— 钉一个建不出来的路径，等于亲手把会话搞挂。
    `.tmp/` 不可用时（上面 try 里已经 warning 过）这里就不管了，让 pytest 走
    默认逻辑：产物可能落到项目根，但至少测试能跑完，`.gitignore` 兜住不提交。

    ---------------------------- 动作 2 ----------------------------
    从子目录跑时，把 `app/` 也加回收集范围。

    踩过的坑（QA 发现）：pytest 只在 **调用目录 == rootdir** 时才用 pytest.ini 的
    `testpaths`：
        # _pytest/config/__init__.py:1415  _decide_args()
        if invocation_dir == rootpath:
            ... result = testpaths ...
        else:
            result = []          # ← 退回"从当前目录递归"
    所以 `cd tests; pytest` 时 `testpaths = tests app` 只生效了一半：`app/` 不收集。
    后果是 `app/errors.py` 里那条 doctest **静默消失**（根目录 88 passed、子目录
    87 passed）—— 而静默少一条回归测试比明着报错更危险，那正是我们开
    `--doctest-modules` 要防的"注释谎报"。

    这里在收集开始前把 `app` 目录补进 `config.args`；但**只在"调用方没给显式目标"
    时补**（即 `file_or_dir` 为空、args 是 pytest 自己按 testpaths/当前目录推出来的）。
    调用方一旦显式点名文件，就让"显式指定"说了算 —— 否则同一个
    `pytest test_health.py` 在根目录 12 个、在 tests/ 里 13 个（多出来的正是 app 的
    doctest），两个入口行为不一致比少跑一条更难查。
    """
    # ---- 动作 1：钉 basetemp（对应上面"动作 1"的说明）----
    if not config.option.basetemp:  # 调用方用 --basetemp 显式指定了就尊重它
        if _TEMP_ENV_SETUP_OK:
            config.option.basetemp = str(TEMP_DIR / "pytest")

    # ---- 动作 2：从子目录跑时补收集 app/ ----
    app_dir = PROJECT_ROOT / "app"
    if not app_dir.is_dir():
        return
    if config.invocation_params.dir == PROJECT_ROOT:
        return  # testpaths 已生效，args 里本来就含 app
    if config.option.file_or_dir:
        return  # 调用方显式点名了目标，尊重它，不偷偷加范围
    for arg in config.args:
        try:
            if Path(str(arg)).resolve() == app_dir.resolve():
                return  # 防御：真的已经在里面了就别重复收集
        except OSError:  # pragma: no cover - 防御性
            continue
    config.args = [*config.args, str(app_dir)]
