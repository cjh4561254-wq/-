r"""环境自检：一条命令回答「我这台机器能不能把这个项目跑起来」。

为什么需要它：本项目的常见故障不是"代码错了"，而是"环境不对"——
用系统 Python 而不是 venv、缺 matplotlib、系统 TEMP 不可写、端口被占、
`.env` 里设了变量却没生效……这些都会以各种奇怪的报错形式出现。
这个脚本把它们一次性检查完，并给出**可执行的修复建议**。

用法（项目根目录）：

    & ".\.venv\Scripts\python.exe" scripts\\check_env.py

退出码：0 = 全部通过（允许有"提醒"）；1 = 有会导致跑不起来的问题。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = "  [ok ]"
WARN = "  [warn]"
BAD = "  [FAIL]"

_failures: list[str] = []
_warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"{OK} {msg}")


def warn(msg: str, fix: str) -> None:
    _warnings.append(msg)
    print(f"{WARN} {msg}")
    print(f"         → 建议：{fix}")


def bad(msg: str, fix: str) -> None:
    _failures.append(msg)
    print(f"{BAD} {msg}")
    print(f"         → 必须修：{fix}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _make_stdout_unencodable_safe() -> None:
    """让一个**编码不了的字符**不再把整个脚本打崩。

    为什么需要：这个脚本的活是"报告环境里的问题"。它的消息里会嵌环境变量原文
    （比如 `TEMP` 的值）。在中文 Windows 上 stdout 常常是 GBK，而错误的路径
    字符串里可能出现 GBK 编不了的字符（实测过 `U+E043` —— 私用区字符，
    是上游某次"把中文路径写坏"留下的），4 个输出函数（`ok`/`warn`/`bad`/
    `section`）又都是裸 `print`，于是**检测器在报告缺陷时自己崩了**：
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\ue043'`，
    退出码 1，用户看到的是回溯而不是那句本来要看的建议。

    改法：把 `errors` 放宽成 `replace` —— 编不了的字符变成 `?`，消息仍可读，
    脚本继续跑完。**注意**两件事：
    1. `sys.stdout` 被重定向时可能**不是** `TextIOWrapper`（没有 `reconfigure`），
       所以要 `getattr` 探一下，拿不到就静默跳过 —— 这条路径不值得再制造一个新异常；
    2. 用户**显式**设了 `PYTHONIOENCODING` 时就别动（那是他的选择），
       只在默认/GBK 的情况下兜底。
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 —— 兜底不该反过来成为新的崩溃点
        pass


def main() -> int:
    _make_stdout_unencodable_safe()
    print(f"项目根 = {ROOT}")
    print(f"Python = {sys.version.split()[0]}  ({sys.executable})")

    # ---------------------------------------------------------------- Python
    section("1) Python 与虚拟环境")
    if sys.version_info < (3, 11):
        bad(
            f"Python {sys.version.split()[0]} 太旧（需要 ≥3.11，本项目用了 3.12 的 X | None 语法）",
            "换用 3.12 重建 venv：python -m venv .venv",
        )
    else:
        ok(f"Python 版本满足要求（{sys.version.split()[0]}）")

    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        if Path(sys.executable).resolve() == venv_python.resolve():
            ok("正在使用项目自带的 venv（.venv\\Scripts\\python.exe）")
        else:
            warn(
                f"当前解释器不是项目 venv（venv 在 {venv_python}）",
                '改用 & ".\\.venv\\Scripts\\python.exe" -m ... 或先激活 venv',
            )
    else:
        warn(
            "没有找到 .venv（可能是你换机器后还没建）",
            'python -m venv .venv; & ".\\.venv\\Scripts\\python.exe" -m pip install -r requirements.txt',
        )

    # ------------------------------------------------------------ 依赖
    # 判据：只有"这个包不在就**做不了核心功能**"的才放这里。密码哈希**不在**
    # 这里 —— 它是标准库实现（见下面"可选依赖"段的注释）。
    section("2) 关键依赖")
    for mod, dist, why in (
        ("fastapi", "fastapi", "服务框架"),
        ("uvicorn", "uvicorn", "ASGI 服务器（起服务用）"),
        ("pydantic", "pydantic", "数据校验"),
        ("pandas", "pandas", "读表与统计（上传数据集必需）"),
        ("matplotlib", "matplotlib", "服务端画图（缺了会没有图表）"),
    ):
        try:
            importlib.import_module(mod)
            try:
                ver = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                ver = "?"
            ok(f"{mod} {ver} — {why}")
        except Exception as exc:  # noqa: BLE001
            bad(
                f"缺 {mod}（{why}）：{exc}",
                '& ".\\.venv\\Scripts\\python.exe" -m pip install -r requirements.txt',
            )
    # 可选依赖。**argon2 曾经被错放在"关键依赖"里**，会打印
    # `[ok] argon2 … 密码哈希（认证必需）` —— 那是错的：本项目的密码哈希是
    # **标准库** `hashlib.pbkdf2_hmac`（`app/core/auth.py:113`，默认 240000 轮
    # PBKDF2-SHA256，无需任何第三方包），`requirements.txt` 与
    # `requirements.lock.txt` 里都没有 argon2。本机之所以"过"，是因为它装在
    # 系统级 Python 里（venv 是 `--system-site-packages`）。**干净机器上会
    # `[FAIL]`，并让用户以为"认证不可用"** —— 一句会害人的错误结论，比不检查更糟。
    #
    # 修法不是"降级成可选"，而是**整条删掉**：既然本项目不依赖 argon2，
    # 就不该检查它 —— 留在清单里只会让人以为它是依赖。
    for mod, dist, why in (
        ("pytest", "pytest", "跑测试"),
        ("httpx", "httpx", "测试用 HTTP 客户端"),
        ("playwright", "playwright", "真实浏览器验收（跑服务不需要）"),
    ):
        try:
            importlib.import_module(mod)
            try:
                ver = importlib.metadata.version(dist)
            except importlib.metadata.PackageNotFoundError:
                ver = "?"
            ok(f"{mod} {ver} — {why}")
        except Exception:  # noqa: BLE001
            warn(
                f"缺可选依赖 {mod}（{why}）",
                "只有做对应的事情时才需要：pip install pytest httpx playwright",
            )

    # ------------------------------------------------------------ 临时目录
    section("3) 临时目录可写性（Playwright / matplotlib 的真实坑）")
    for label, env_name in (("TEMP", "TEMP"), ("TMP", "TMP")):
        val = os.environ.get(env_name, "")
        if not val:
            warn(f"环境变量 {label} 为空", '指向项目内目录：$env:TEMP="$PWD\\.tmp\\pwtemp"')
            continue
        try:
            with tempfile.TemporaryDirectory(dir=val) as d:
                probe = Path(d) / "probe.txt"
                probe.write_text("ok", encoding="utf-8")
            ok(f"{label} 可写 = {val}")
        except Exception as exc:  # noqa: BLE001
            warn(
                f"{label} 不可写（{val}）：{type(exc).__name__}: {exc}"
                " —— Playwright 会报 EPERM mkdtemp",
                '$env:TEMP="$PWD\\.tmp\\pwtemp"; $env:TMP=$env:TEMP',
            )

    mpl = os.environ.get("MPLCONFIGDIR", "")
    if mpl:
        ok(f"MPLCONFIGDIR = {mpl}")
    else:
        warn(
            "没有设 MPLCONFIGDIR（matplotlib 会往用户目录写缓存，权限不足时会报 mkdir -p failed）",
            '$env:MPLCONFIGDIR="$PWD\\.tmp\\mplcache"',
        )

    # ------------------------------------------------------------ 目录与库
    section("4) 数据落点")
    try:
        from app.config import get_settings

        s = get_settings()
        print(f"  [-- ] DB_PATH      = {getattr(s, 'db_path', '(未暴露)')}")
        print(f"  [-- ] AUTH_DB_PATH = {getattr(s, 'auth_db_path', '(未暴露)')}")
        print(f"  [-- ] UPLOAD_DIR   = {getattr(s, 'upload_dir', '(未暴露)')}")
        print(f"  [-- ] CHARTS_DIR   = {getattr(s, 'charts_dir', '(未暴露)')}")
        print(f"  [-- ] 单文件上限    = {getattr(s, 'max_upload_mb', '?')} MB")
        print(f"  [-- ] 最大行数      = {getattr(s, 'max_rows', '?')}")
        print(f"  [-- ] 大模型已配置  = {bool(getattr(s, 'llm_api_key', ''))}")
        print("         （不配大模型也能用：报告走规则模板生成，界面会标出来源）")
    except Exception as exc:  # noqa: BLE001
        bad(f"读配置失败：{exc}", "确认在项目根目录运行，且 app/config.py 未被改坏")

    try:
        from app.data.users import get_auth_store

        store = get_auth_store()
        store.ensure_schema()
        n = store.count_users()
        ok(f"账号库可访问（{n} 个账号）")
        if n == 0:
            print(
                "         → 库是空的：第一次打开页面会被带到 /login?bootstrap=1，"
                "可以在那里建第一个管理员。"
            )
    except Exception as exc:  # noqa: BLE001
        bad(f"账号库不可用：{exc}", "检查 DB_PATH / AUTH_DB_PATH 指向的目录是否存在且可写")

    # ------------------------------------------------------------ 端口
    section("5) 端口占用")
    for port in (8000, 8100, 8130):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                ok(f"{port} 空闲")
            except OSError:
                warn(
                    f"{port} 已被占用",
                    f'换端口，或先查占用：Get-NetTCPConnection -LocalPort {port} -State Listen',
                )

    # ------------------------------------------------------------ 前端
    section("6) 前端文件（应为 7 个，无构建步骤）")
    expected = [
        "index.html",
        "app.css",
        "app.js",
        "tokens.css",
        "login.html",
        "login.css",
        "login.js",
    ]
    web = ROOT / "app" / "web"
    for name in expected:
        p = web / name
        if p.exists():
            ok(f"app/web/{name}  {p.stat().st_size:,} B")
        else:
            bad(f"缺 app/web/{name}", "从版本库恢复该文件（前端是静态文件，不需要构建）")
    node = __import__("shutil").which("node")
    if node:
        ok(f"node 可用（{node}）—— 仅用于 node --check 语法检查，跑服务不需要")
    else:
        warn("没有 node（只在做 JS 语法检查时需要）", "可跳过；服务本身不需要 Node")

    # ------------------------------------------------------------ 汇总
    section("汇总")
    if _failures:
        print(f"发现 {len(_failures)} 个必须修的问题：")
        for f in _failures:
            print(f"  - {f}")
    if _warnings:
        print(f"另有 {len(_warnings)} 条提醒（不一定影响运行）：")
        for w in _warnings:
            print(f"  - {w}")
    if not _failures:
        print("结论：环境可以跑起来。" + ("（提醒项按需处理）" if _warnings else ""))
        return 0
    print("结论：先把上面的 FAIL 修掉再起服务。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
