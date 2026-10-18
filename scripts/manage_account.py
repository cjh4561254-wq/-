r"""本项目的账号运维脚本：建号 / 改密 / 列账号 / 停用账号。

为什么需要它：本项目**没有自助注册**（刻意的产品决策），所以第二个账号、以及
"忘了密码"，只能从服务端做。这个脚本把这件事变成一条可复制的命令，
并且**把会话一起处理掉**（改密后旧会话必须失效，否则等于没改）。

用法（在项目根目录下跑）：

    # 建号（密码从环境变量读，避免出现在命令历史里）
    $env:MIS_NEW_PASSWORD = "你的密码"
    & ".\.venv\Scripts\python.exe" scripts\manage_account.py create --username analyst_li --display-name "李分析"

    # 改密（会同时踢掉该账号所有已登录会话）
    $env:MIS_NEW_PASSWORD = "新密码"
    & ".\.venv\Scripts\python.exe" scripts\manage_account.py passwd --username analyst_li

    # 列账号
    & ".\.venv\Scripts\python.exe" scripts\manage_account.py list

    # 停用 / 启用
    & ".\.venv\Scripts\python.exe" scripts\manage_account.py disable --username analyst_li

数据库位置：跟随环境变量 `AUTH_DB_PATH`（其次 `DB_PATH`），不设就是项目根的 `data/db/auth.db`。
脚本不会打印任何密码或哈希。

退出码：0 成功；1 业务失败（重名/找不到）；2 参数或密码不合法；3 缺少能力时如实拒绝。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 按路径跑脚本时，Python 只把脚本所在目录加进 sys.path —— 必须自己把项目根加进去，
# 否则 `import app` 会 ModuleNotFoundError。
sys.path.insert(0, str(ROOT))

from app.core.auth import hash_password  # noqa: E402
from app.data.users import AuthStoreError, get_auth_store  # noqa: E402


def _store():
    """拿账号库。

    用 `get_auth_store()`（配置层决定的单例），而**不是**自己 `AuthStore(...)`：
    自己 new 一个会绕过 `AUTH_DB_PATH` 的解析，跑到默认的 `data/db/auth.db` 上去，
    于是"我明明改了密码，怎么登录还是旧密码"。这个坑本项目真踩过
    （`get_auth_store` 曾因 `lru_cache` 不带参数而在导入期绑死配置）。
    """
    return get_auth_store()


def _password_from_env() -> str:
    """从环境变量取密码，并**用应用层那把尺子**量一遍。

    这里曾经是自己写死 `min_len = 8`：数值恰好等于应用层的
    `PASSWORD_MIN_LENGTH`（`app/schemas_auth.py:25`），所以**看起来**没问题。
    但那是"两处各写一个 8"，今天相同纯属巧合 —— 哪天后端把最小长度改成 10，
    这个脚本会放着 8 位密码过去，用户会看到一个"明明不符合规则却建号成功"的账号。
    更糟的是那 8 只检查长度：`"12345678"`（纯数字）以前能从脚本建出来，
    却过不了应用层的 `validate_password_strength`。

    现在直接调应用层那个函数（`PASSWORD_MIN_LENGTH` / 长度上限 / 常见弱密码 /
    必须同时含字母和数字，全在里面，规则只在那一处定义），
    判据因此**只会跟着应用层变**，不会各走各的。它抛 `ValueError` 时退出码仍是 2
    （与脚本文档、`docs/runbook.md:291` 的"`2` 密码不合法"一致）。
    """
    pw = os.environ.get("MIS_NEW_PASSWORD", "")
    if not pw:
        print(
            "错误：没有拿到密码。请先设置环境变量 MIS_NEW_PASSWORD，例如：\n"
            '    $env:MIS_NEW_PASSWORD = "你的密码"',
            file=sys.stderr,
        )
        raise SystemExit(2)
    # 在**函数里**import 而不是模块顶层：这条判据要在"跑的那一刻"读应用层的
    # 规则，而不是在 import 脚本的那一刻把它绑死（`get_auth_store()` 当年就因为
    # `lru_cache` 在导入期绑死配置踩过坑，注释见本文件 `_store()`）。
    from app.schemas_auth import validate_password_strength

    try:
        validate_password_strength(pw)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    return pw


def cmd_create(args: argparse.Namespace) -> int:
    store = _store()
    store.ensure_schema()
    password = _password_from_env()
    ph = hash_password(password)
    try:
        rec = store.create_user(
            username=args.username,
            display_name=args.display_name or args.username,
            password_hash=ph.hash_hex,
            salt=ph.salt_hex,
            iterations=ph.iterations,
        )
    except AuthStoreError as exc:
        print(f"建号失败：{exc}", file=sys.stderr)
        return 1
    print(f"建号成功：id={rec.id} username={rec.username} display_name={rec.display_name}")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    store = _store()
    rec = store.find_user(args.username)
    if rec is None:
        print(f"改密失败：找不到账号 {args.username}", file=sys.stderr)
        return 1
    password = _password_from_env()
    ph = hash_password(password)
    store.update_password(
        rec.id,
        password_hash=ph.hash_hex,
        salt=ph.salt_hex,
        iterations=ph.iterations,
    )
    # 关键的一步：改完密码必须把旧会话全部作废，否则别人拿着旧 Cookie 还能继续用。
    killed = store.delete_user_sessions(rec.id)
    print(f"改密成功：username={rec.username}，已作废该账号的 {killed} 个已登录会话")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    store.ensure_schema()
    users = []
    # UserStore 没有 list_users，这里按 id 递增探测（账号数很少，够用且不依赖私有接口）。
    uid = 1
    misses = 0
    while misses < 50 and uid < 10000:
        rec = store.get_user(uid)
        if rec is None:
            misses += 1
        else:
            misses = 0
            users.append(rec)
        uid += 1
    if not users:
        print("当前用户库为空（可以在登录页走 ?bootstrap=1 建第一个管理员）。")
        return 0
    print(f"{'id':>4}  {'username':<24} {'display_name':<24} sessions  last_login_at")
    for rec in users:
        sessions = store.count_sessions(rec.id)
        print(
            f"{rec.id:>4}  {rec.username:<24} {str(rec.display_name):<24} "
            f"{sessions:>8}  {rec.last_login_at or '—'}"
        )
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    store = _store()
    rec = store.find_user(args.username)
    if rec is None:
        print(f"找不到账号 {args.username}", file=sys.stderr)
        return 1
    setter = getattr(store, "set_active", None)
    if setter is None:
        print(
            "该版本的 UserStore 没有 set_active 方法 —— 本脚本不做 SQL 直改，"
            "请在评审时改用数据库工具并自行核对影响面。",
            file=sys.stderr,
        )
        return 3
    setter(rec.id, not args.enable)
    if not args.enable:
        killed = store.delete_user_sessions(rec.id)
        print(f"已停用 {rec.username}，并作废 {killed} 个会话")
    else:
        print(f"已启用 {rec.username}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="账号运维（建号/改密/列账号/停用）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="建一个账号")
    p_create.add_argument("--username", required=True)
    p_create.add_argument("--display-name", default="")
    p_create.set_defaults(func=cmd_create)

    p_passwd = sub.add_parser("passwd", help="改密码（并作废旧会话）")
    p_passwd.add_argument("--username", required=True)
    p_passwd.set_defaults(func=cmd_passwd)

    p_list = sub.add_parser("list", help="列出账号")
    p_list.set_defaults(func=cmd_list)

    p_disable = sub.add_parser("disable", help="停用/启用账号")
    p_disable.add_argument("--username", required=True)
    p_disable.add_argument("--enable", action="store_true", help="改成启用")
    p_disable.set_defaults(func=cmd_disable)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
