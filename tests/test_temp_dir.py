"""临时文件到底落到哪儿 —— 钉住一条"不崩但会往仓库里扔垃圾"的性质。

为什么值得单独一个测试文件
--------------------------
本机 Python 子进程**写不进系统临时目录**（`%TEMP%`、`C:\\Windows\\Temp` 全部
PermissionError），`tempfile` 试完所有候选目录后会退回**当前工作目录**。而本项目
的"当前工作目录"通常就是仓库根目录，于是临时文件直接掉进仓库 —— 一个
`git add -A` 就可能提交进去（那正是 docs/DECISIONS.md D-05 记录过的事故）。

这条性质的危险之处在于**它不会让任何功能报错**：上传成功、测试全绿，只是仓库
慢慢变脏。所以只能靠测试钉住。

现在的机制（2026-10-13 重构后）
------------------------------
临时目录由 `app/runtime.py` 的 `runtime_environment()` 在**服务启动时**显式设置：
`app/main.py` 的 lifespan 包住 `yield`，把 `TEMP`/`TMP`/`TMPDIR` 指到
`settings.temp_path`（默认项目内 `.tmp/`，可用 `TEMP_DIR` 覆盖），退出时恢复现场。

**为什么不用"import app 时设置环境变量"那套**（曾经这么写过，被推翻）：
1. `import` 一个包就改进程全局环境，副作用太脏，而且"谁先 import 了谁"决定行为；
2. 更实际的是它**猜错过**：早期版本看到 `TEMP` 已设就尊重它，而这台机器上
   `TEMP` 指向的目录**存在但写不进去**，于是临时文件照旧掉回仓库根，全程无报错。
   现在改成"应用明确指定自己的临时目录"，不继承、不猜测系统配置。

为什么必须起子进程来测
----------------------
在**本测试进程内**测不出真实入口的行为：pytest 启动时 `conftest.py` 已经把
`TEMP`/`TMP`/`TMPDIR` 指到 `.tmp/` 了，所以进程内怎么看都是对的 —— 不管
`runtime_environment()` 有没有生效。要看"一个干净启动的进程会怎么选"，
只能另起一个进程，把三个变量换成**不可用的值**再让它自己决定。

⚠️ 断言刻意做成与"本机系统临时目录可不可写"无关：
   - 启动之后 → 必须是项目内 `.tmp/`（任何机器上都应成立）；
   - 启动之前 → 必须**不是**项目内 `.tmp/`（否则说明有别的东西在偷偷设置，
     这条测试就失去判别力）。
不写 `assert == cwd`：那依赖"所有候选目录都失败"，在系统临时目录可写的机器上
不成立，会让测试变成"只在这台机器上绿"。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from app.config import PROJECT_ROOT

# 期望的落点：项目根下的 .tmp/。测试进程与子进程算出来的必须一致。
EXPECTED_TEMP_DIR = (PROJECT_ROOT / ".tmp").resolve()

# 让子进程看到一个"不可用"的系统临时目录：不存在的路径。
# 用不存在的路径而不是"权限不足的路径"，是为了在任何操作系统上都能构造，
# 且效果一样（tempfile 探测时写不进去）。
UNUSABLE_TEMP = str(PROJECT_ROOT / ".tmp" / "definitely-not-a-real-dir" / "temp")


def _run_in_clean_process(code: str, cwd: Path) -> list[str]:
    """起一个干净子进程跑 code，返回它输出的每一行（已 strip）。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("TEMP", "TMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT")
    }
    # 三个变量都指向不可用的目录：子进程必须自己解决"临时文件放哪"。
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = UNUSABLE_TEMP
    # `PYTEST_DEBUG_TEMPROOT` 必须删掉：pytest 用它把 basetemp 钉在 `<TEMP>/pytest`，
    # 而 `tempfile.gettempdir()` 在 basetemp 存在时**优先返回 basetemp** ——
    # 不删的话子进程会直接继承 pytest 的临时目录答案，"有没有启动应用"看起来
    # 完全一样，用例失去判别力（踩过一次）。
    #
    # cwd 故意不是项目根，所以子进程默认 import 不到 app 包，得显式把项目根放进
    # 搜索路径。这一步是"为了让它能 import"，不是"为了让测试通过"：真实入口
    # （`python -m uvicorn app.main:app`）也是从项目根启动的。
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    # 不 import app 的那条也用同一个解释器，避免"测的是全局 Python"这种误会。
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"子进程非零退出：{completed.returncode}\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"子进程没有输出任何内容\nstderr={completed.stderr}"
    return lines


def test_startup_pins_temp_files_into_the_project(tmp_path):
    """服务启动后，临时文件必须落在项目内的 `.tmp/`。

    走的是**真实入口**：`import app.main` + `with TestClient(app)`（等于跑一遍
    lifespan），不是直接调用 `runtime_environment()` —— 否则测的是"我调了这个
    函数"，而不是"服务启动时会这么做"。

    同时验证两件事，它们的失败含义完全不同：
      - 启动**前** `TEMP` 不能被 import 悄悄改掉（import 不该有全局副作用）；
      - 启动**中** `tempfile.gettempdir()` 必须是 `.tmp/`；
      - 退出上下文后环境必须恢复原样（测试里反复进出不会互相污染）。
    """
    code = f"""
        import os, tempfile

        original = {{name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}}
        print("BEFORE_IMPORT=" + original["TEMP"])

        import app  # noqa: F401
        print("AFTER_IMPORT=" + os.environ["TEMP"])
        # 三个变量都要查，不能只看 TEMP：只查一个的话，另外两个被改动也发现不了。
        print("AFTER_IMPORT_ALL=" + ",".join(
            f"{{name}}={{os.environ.get(name)}}" for name in ("TEMP", "TMP", "TMPDIR")
        ))

        from fastapi.testclient import TestClient
        from app.main import app as application

        with TestClient(application):
            print("INSIDE=" + tempfile.gettempdir())

        print("AFTER_EXIT=" + os.environ["TEMP"])
    """
    lines = _run_in_clean_process(code, cwd=tmp_path)
    # 子进程还会打印 lifespan 自己的启动日志（"[startup] ..."），所以只挑我们要的键。
    # 不按顺序取前四行：那会让"多打一行日志"变成测试失败，看起来像功能坏了。
    keys = ("BEFORE_IMPORT", "AFTER_IMPORT", "AFTER_IMPORT_ALL", "INSIDE", "AFTER_EXIT")
    values = {
        key: line.split("=", 1)[1]
        for line in lines
        for key in keys
        if line.startswith(f"{key}=")
    }
    assert set(values) == set(keys), f"子进程输出缺少标记：拿到 {sorted(values)}，原始输出={lines}"

    # ① import 不该改临时目录相关变量：import 有全局副作用是上一版被推翻的做法。
    #    （`MPLCONFIGDIR` 是唯一有意的例外 —— matplotlib 在 import 时就决定配置目录，
    #    等不到服务启动。那条由 app/config.py 的注释和 test_import_side_effects 说明。）
    assert values["AFTER_IMPORT"] == values["BEFORE_IMPORT"] == UNUSABLE_TEMP, (
        "import app 竟然改动了 TEMP —— import 不应有这种全局副作用"
    )
    assert values["AFTER_IMPORT_ALL"] == (
        f"TEMP={UNUSABLE_TEMP},TMP={UNUSABLE_TEMP},TMPDIR={UNUSABLE_TEMP}"
    ), f"import app 改动了 TMP/TMPDIR：{values['AFTER_IMPORT_ALL']}"

    # ② 启动之后必须落在项目内 .tmp/（这条是本次要钉的性质）。
    assert Path(values["INSIDE"]).resolve() == EXPECTED_TEMP_DIR, (
        f"服务启动后临时目录应为 {EXPECTED_TEMP_DIR}，实际是 {values['INSIDE']!r}"
        "（说明 lifespan 里那段 runtime_environment 没生效）"
    )

    # ③ 退出后恢复现场：否则同一个进程里第二次启动会继承上一次的残留。
    assert values["AFTER_EXIT"] == UNUSABLE_TEMP, (
        f"退出 lifespan 后 TEMP 应恢复为 {UNUSABLE_TEMP!r}，实际 {values['AFTER_EXIT']!r}"
    )


def test_without_startup_tempfiles_do_not_go_into_the_project_dir(tmp_path):
    """对照：**没启动应用**时，临时目录不是"启动后该是"的那一个。

    这条是上面那条的对照，用来说明"落到 .tmp"确实来自启动逻辑，而不是碰巧。

    **它到底证明了什么（别把它的判别力想大了）**：断言是"不**恰好等于**
    `TEMP_DIR`"，不是"不在项目内"。这两种写法差别很大：

    - 本机（Windows）系统临时目录不可写，`tempfile` 退回 cwd，而本用例的
      子进程 cwd 是 `tmp_path`，即 `<root>/.tmp/pytest/<用例名>/` —— 也就是
      **它本来就在项目 `.tmp/` 之下**，只是不等于 `TEMP_DIR` 这个精确路径。
    - Linux 上会退到 `/tmp`，连"在 .tmp 之下"都不成立。

    QA 在 24aeac1 上插桩实测过这件事（打印出子进程自己看到的 gettempdir，
    确认就在 `.tmp/pytest/...` 里）。所以**这条用例的判别力是平台相关的**，
    原先注释里"断言必须与机器配置无关"的说法不成立，已改掉。

    为什么仍然保留精确相等而不是改成 `EXPECTED_TEMP_DIR not in resolved.parents`：
    后者在 Windows 上会立刻红，而红的不是缺陷、是本机的 cwd 兜底事实。
    真正要抓的"有人在 import 时刻就把 TEMP 设成 `TEMP_DIR`"由上面那条用例的
    第 ① 步（import 前后逐键比较环境变量）负责，那条与平台无关。
    """
    lines = _run_in_clean_process(
        "import tempfile; print(tempfile.gettempdir())", cwd=tmp_path
    )
    resolved = Path(lines[-1]).resolve()

    assert resolved != EXPECTED_TEMP_DIR, (
        "没启动应用时临时目录就已经是项目内 .tmp/ —— 说明有别的东西在设置它，"
        "本用例失去判别力（要检查 conftest 或某个 import 副作用）"
    )


def test_runtime_environment_restores_previous_process_state(tmp_path, monkeypatch):
    """进程内直接进出一遍启动上下文：目录被建出来、env 被设上、退出后还原。

    与上面两条的分工：那两条验"真实入口的行为"，这条验**边界细节**——
    退出时有没有把 `os.environ` 和 `tempfile` 的缓存恢复干净。没恢复的话，
    同一个进程里第二次启动会带着上一次的残留，测试之间也会互相污染。
    """
    from app import config
    from app.runtime import runtime_environment

    target = tmp_path / "app-temp"
    monkeypatch.setenv("TEMP_DIR", str(target))
    config.reset_settings_cache()

    env_before = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    cache_before = tempfile.tempdir

    with runtime_environment(config.get_settings()) as yielded:
        assert yielded == target.resolve()
        assert target.is_dir(), "启动上下文必须把临时目录真的建出来"
        assert Path(tempfile.gettempdir()).resolve() == target.resolve()

    assert {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")} == env_before
    assert tempfile.tempdir == cache_before
    config.reset_settings_cache()
