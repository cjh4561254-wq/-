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


# ── 认证：给测试会话一个**确定存在**的账号库 ────────────────────────────────
# 背景：登录功能给业务路由挂上了 `Depends(require_user)`，没配账号库时
# `get_auth_store()` 返回 None，路由层会翻译成 503 —— 那样几十个业务用例会以
# "服务不可用"收场，看起来像功能坏了，实际是**测试环境没给账号库**。
#
# 所以这里在导入任何 app 模块之前先把 `AUTH_DB_PATH` 钉到 `.tmp/auth-test/auth.db`，
# 于是模块级的 `AUTH_DB_PATH = os.getenv(...)` 读到的就是一个固定文件。
#
# ⚠️ **必须写成相对路径**（2026-10-16 实修，别改回 `(TEMP_DIR / ...).resolve()`）：
# `Settings.auth_db_file` 是按**当前进程的 PROJECT_ROOT** 解析相对路径的，所以
# 相对值在"真仓库"里落到真仓库的 `.tmp/`，在 `tests/test_check_docs.py` 造的
# **镜像仓库**里就落到镜像的 `.tmp/` —— 一份环境变量，两处正确落点。
# 写成绝对路径会踩两个坑，都实测到了：
#   1. **跨仓库串库**。`_child_env()` 复制 os.environ，于是镜像子进程拿着真仓库的
#      绝对路径去连真仓库的 `.tmp/auth-test/auth.db`（实测镜像日志里出现
#      `账号库就绪: D:\个人项目01\ai-data-analyst-agent\.tmp\auth-test\auth.db`）。
#      镜像的隔离性从此是假的：它的绿灯依赖真仓库里那份文件的状态。
#   2. **镜像里的业务用例集体 401**。镜像只有空账号库（真库不在镜像里），
#      `resolve_session()` 查不到会话 → `check_docs.py` 的 8 个投喂用例全红 →
#      12 个镜像变异用例连锁 ERROR。
#
# `.tmp/auth-test` 这一级目录**不能省**：`AuthStore` 的测试辅助代码会按真实路径
# 造账号/签会话，而目录由 `AuthStore._connect()` 用 parents=True 自建；
# 但 `data/db/auth.db` 那种"父目录被别的东西占成文件"的情况没必要在这里重演。
AUTH_DB_PATH = Path(".tmp") / "auth-test" / "auth.db"
if _TEMP_ENV_SETUP_OK:
    os.environ["AUTH_DB_PATH"] = str(AUTH_DB_PATH)


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


# ── 环境隔离：大模型配置不许受**本机真实环境变量**影响 ──────────────────────
# 这条是踩出来的，不是想出来的：
#   用户机器上真的有 `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`（机器级环境变量），
#   而配置层从 D4 收尾起认这两个名字。于是所有"未配置"用例（例如断言
#   `Settings().llm_configured is False`、断言没配模型时必须抛 `LLMNotConfigured`）
#   在**没 key 的机器上绿、在有 key 的机器上红** —— 而后者才是用户的机器。
#   这类失败看起来像代码坏了，其实是测试把"我这台机器碰巧没配"当成了前提。
#
# 做法：每个用例开始前把六个名字全部清空。清单只有一份
# （`app.config.LLM_ENV_VAR_NAMES`，与字段声明共用同一组常量），测试不另抄一份 ——
# 抄的那份迟早漏一个，而漏掉的那个正好就是本机真实存在的名字。
#
# 用例想用环境变量就自己 `monkeypatch.setenv`：本夹具先跑、用例后设，顺序天然正确。
@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch):
    """清掉大模型相关的六个环境变量名，并在前后各清一次配置缓存。"""
    from app import config

    # 在这里就抓住真正的清缓存函数，而**不是在拆除阶段调用
    # `config.reset_settings_cache()`：那个函数内部会去调 `get_settings.cache_clear()`，
    # 而个别用例会把 `config.get_settings` 换成假对象（见
    # tests/test_planner_rules.py::test_llm_is_configured_reads_settings_at_call_time），
    # 夹具拆除又发生在 monkeypatch 还原**之前** —— 于是拆除阶段拿到的是那个假对象、
    # 没有 cache_clear，用例明明通过了却报 ERROR（实测踩到：2 failed 里有 1 个 ERROR）。
    # 夹具只该清"真实存在的那份缓存"，所以这里持有真函数的引用。
    clear_settings_cache = config.get_settings.cache_clear

    for name in config.LLM_ENV_VAR_NAMES:
        monkeypatch.delenv(name, raising=False)
    # 清缓存不能省：`get_settings()` 带 `lru_cache`，不清的话上一个用例读到的
    # 配置会留给下一个用例（本项目真踩过这个坑）。
    clear_settings_cache()
    yield
    clear_settings_cache()


# ── 认证：让 `TestClient` 默认"已登录" ──────────────────────────────────────
# 为什么是**补丁 TestClient**而不是"改 30 处调用点"：
#   登录功能上线后，`TestClient(app)` 不再能直接访问业务路由（401）。要让它们通过，
#   有两条路：① 把 30 多个测试文件里的 client 逐个换成"已登录的 client"；
#   ② 在测试环境里让"新建的 client"默认带上一条真实会话。
#   选 ②，理由有三条：
#     1. 用例的**意图**是"测某个业务行为"，不是"测未登录会被拒"。让每个人先写三行
#        登录样板，只会让真正要测的断言离题更远。
#     2. 未登录行为有**专门的**用例覆盖：tests/test_auth_api.py 里显式用
#        `TestClient(app, cookies={...})` 构造"没 cookie / 坏 cookie / 过期 cookie"
#        三种客户，需求里"未登录必须 401"这条断言强度一点没降。
#     3. 改 30 个文件会同时踩到别的队友正在改的文件（并行写冲突）。
#
# 补丁只在**测试进程内**生效：它改的是 `starlette.testclient.TestClient` 这个类对象，
# 产品代码（uvicorn 起的 app）碰不到它。
#
# 覆盖 `request` 而不是 `__init__`：模块级 `client = TestClient(app)` 在**收集阶段**
# 就构造好了，那时账号库还不一定有（用例自己的 fixture 可能换路径），
# 所以在"真的要发请求"的那一刻再补 cookie 最稳。`__enter__` 不重写 —— lifespan
# 由原来的实现照常跑，我们只是保证 cookie 在 jar 里。
def _patch_testclient_for_auth() -> None:
    from starlette.testclient import TestClient

    from tests import auth_support

    if getattr(TestClient, "_dsh_auth_patched", False):  # pragma: no cover - 防御
        return

    original_request = TestClient.request

    def request(self, *args, **kwargs):
        # `mark_client_as_anonymous()` 打上的标记：这个 client 就是要"没登录"，
        # 不许补会话（否则 `cookies.clear()` 会被下一次请求重新填满）。
        if not getattr(self, "_dsh_no_auto_session", False):
            auth_support.attach_session_cookie(self)
        return original_request(self, *args, **kwargs)

    TestClient.request = request
    TestClient._dsh_auth_patched = True  # type: ignore[attr-defined]


# autouse 但**不取任何夹具参数**：它只挂补丁，不碰账号库。
# 账号库是按需建的（第一个真发请求的用例里建），这样"只跑一条纯函数用例"时
# 不必付一次 PBKDF2 的代价。
@pytest.fixture(autouse=True, scope="session")
def _auto_login_test_clients():
    """整个测试会话内，新建的 `TestClient` 都自动带上测试账号的真实会话。"""
    if _TEMP_ENV_SETUP_OK:
        _patch_testclient_for_auth()
    yield


@pytest.fixture(autouse=True)
def _reset_auth_store_cache():
    """每个用例前后各清一次账号库缓存。

    与上面清 settings 缓存同理：`get_auth_store()` 带 `lru_cache`，而用例可能
    `monkeypatch.setenv("AUTH_DB_PATH", ...)` 换成临时库；不清的话下一个用例
    还在用上一个用例的库（本项目在配置缓存上真踩过这个坑）。
    """
    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    yield
    reset_auth_store_cache()
