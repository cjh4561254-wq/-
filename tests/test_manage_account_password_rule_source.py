"""守护测试：账号运维脚本不许**自己抄一份**应用的密码规则。

**背景（task-28 缺陷 D，Lead 裁定叫"危险的一致"）**：`scripts/manage_account.py`
曾经在自己的 `_password_from_env()` 里写死 `min_len = 8`，数值恰好等于
应用层的 `PASSWORD_MIN_LENGTH`（`app/schemas_auth.py:25`）—— **今天相同纯属巧合**。
哪天后端把最小长度改成 10，脚本会放着 8 位密码过去，用户看到一个
"明明不符合规则却建号成功"的账号。同一处还有更宽的漏洞：那 8 只量长度，
`"12345678"`（纯数字、且在常见弱密码表里）以前能从脚本建出来，
却过不了应用层的 `validate_password_strength`。

**这条测试钉的是"规则从哪来"，不是"规则是多少"**：
如果断言"脚本里的下限 == `PASSWORD_MIN_LENGTH`"，那么在源码里留一个
`min_len = PASSWORD_MIN_LENGTH` 也能过 —— 那仍然是一份副本，改起来照样会漂。
所以这里分三层：

1. **结构级**（AST）：脚本里没有 `min_len = <值>` 这类本地下限赋值。
2. **行为级**：换掉应用层那把尺子，脚本的判据必须跟着变 —— 跟着变才算"取来的"。
3. **一致性**：应用层拒绝的密码，脚本也必须拒绝（覆盖上面那个纯数字漏洞）。

行为级那两条只调 `_password_from_env()`，它在**任何数据库操作之前**就返回，
所以不会碰 `data/db/auth.db`。
"""

from __future__ import annotations

import ast
import importlib.util

import pytest

from app import config

SCRIPT = config.PROJECT_ROOT / "scripts" / "manage_account.py"

#: 这些名字一旦出现"赋值"，就是在抄应用层的判据。
_COPIED_LIMIT_NAMES = {"min_len", "MIN_LEN", "min_length", "MIN_PASSWORD_LENGTH"}


def _script_ast() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def test_script_does_not_hardcode_a_password_minimum_length() -> None:
    """脚本里不许出现 `min_len = 8` 这种"抄来的"下限。"""
    offenders: list[str] = []

    for node in ast.walk(_script_ast()):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = target.id if isinstance(target, ast.Name) else None
            if name in _COPIED_LIMIT_NAMES:
                offenders.append(f"第 {node.lineno} 行：{name} = <{type(node.value).__name__}>")

    assert not offenders, (
        "脚本自己抄了一份密码最小长度（应该从应用层取）：\n" + "\n".join(offenders)
    )


def test_script_has_no_leftover_dead_import() -> None:
    """那条 `del get_settings` 的死代码不许回来。

    它曾经长这样：`from app.config import get_settings` 紧跟着 `del get_settings`，
    什么也没做 —— 但它让读者以为脚本会跟随应用层配置。只在**代码**里找，
    不扫 docstring（docstring 里正当地提到了这个来源）。
    """
    tree = _script_ast()
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "get_settings" not in names, "脚本里又出现了 get_settings（它不读这个来源）"
    assert not any(isinstance(node, ast.Delete) for node in ast.walk(tree)), (
        "脚本里又出现了 `del` 死代码"
    )


def test_script_calls_the_application_layer_rule() -> None:
    """脚本必须**调用**应用层那把尺子，而不是自己实现一遍。"""
    assert "validate_password_strength" in SCRIPT.read_text(encoding="utf-8"), (
        "脚本没有调用应用层的 validate_password_strength —— 密码规则会各走各的"
    )


def _load_script_as_module():
    """把脚本当模块 import（脚本自己会把项目根塞进 `sys.path`）。"""
    spec = importlib.util.spec_from_file_location("_guard_manage_account", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _password_exit_code(password: str, monkeypatch: pytest.MonkeyPatch) -> int:
    """调脚本的 `_password_from_env()`，返回退出码（0 = 通过）。

    这条路径在**任何数据库操作之前**就返回，所以不会碰 `data/db/auth.db`；
    仍然显式把 `AUTH_DB_PATH` 指到项目内的临时路径，避免任何意外。
    """
    module = _load_script_as_module()
    monkeypatch.setenv("MIS_NEW_PASSWORD", password)
    monkeypatch.setenv(
        "AUTH_DB_PATH", str(config.PROJECT_ROOT / ".tmp" / "backend5" / "guard_auth.db")
    )
    try:
        module._password_from_env()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_script_rejects_a_password_that_is_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """太短的密码 → 退出码 2（与脚本文档、runbook 的"`2` 密码不合法"一致）。"""
    assert _password_exit_code("ab1", monkeypatch) == 2


def test_script_rejects_a_password_the_application_layer_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """纯数字的 8 位密码：应用层拒绝它 → 脚本也必须拒绝。

    这条就是缺陷 D 里那个更宽的漏洞：旧脚本只量长度，所以 `"12345678"`
    （在应用层的常见弱密码表里）能建号成功，而应用层会拒。两条判据必须一致。
    """
    from app.schemas_auth import PASSWORD_MIN_LENGTH, validate_password_strength

    password = "1" * PASSWORD_MIN_LENGTH
    with pytest.raises(ValueError):
        validate_password_strength(password)  # 前提：应用层确实拒绝它

    assert _password_exit_code(password, monkeypatch) == 2, (
        "应用层拒绝了这个密码，脚本却放过了"
    )


def test_script_accepts_a_password_the_application_layer_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向用例：应用层接受的密码必须通过。

    没有这条，上面几条"永远拒绝"的实现也能全绿。
    """
    from app.schemas_auth import validate_password_strength

    password = "analyst2026x"
    assert validate_password_strength(password) == password  # 前提

    assert _password_exit_code(password, monkeypatch) == 0


def test_password_check_follows_the_application_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把应用层那把尺子**换掉**，脚本的判据必须跟着变 —— 这是"取来的"的证明。

    做法：把 `app.schemas_auth.validate_password_strength` 换成一个
    "只接受 `superlongpassword`"的替身，再看脚本的 `_password_from_env()`。
    脚本若自己抄了一份规则，替身对它无效，它会把 `abc12345` 放过去；
    脚本若调用应用层函数（且是**调用时**才 import），替身生效，它会以退出码 2 拒绝。

    这条同时钉住了"函数里 import 而不是模块顶层 import"这个细节：
    顶层 import 会把规则在**脚本 import 那一刻**绑死，替身就换不动了
    （`get_auth_store()` 当年就因为 `lru_cache` 在导入期绑死配置踩过坑）。
    """
    from app import schemas_auth

    def only_one_password_is_valid(value: str) -> str:
        if value != "superlongpassword":
            raise ValueError("替身规则：只有 superlongpassword 合法")
        return value

    monkeypatch.setattr(
        schemas_auth, "validate_password_strength", only_one_password_is_valid
    )

    assert _password_exit_code("abc12345", monkeypatch) == 2, (
        "替身规则拒绝了 abc12345，脚本却放过了 —— 说明它用的是自己抄的规则"
    )
    # 反向：替身认可的密码必须通过，否则上一条可能只是"永远拒绝"。
    assert _password_exit_code("superlongpassword", monkeypatch) == 0
