"""认证的端到端测试：初始化 → 登录 → 受保护接口 → 改密码 → 退出。

这份文件补的是"登录功能上线后，认证这一层到底有没有真的挡住人"。
之所以要单独写一份，是因为 **conftest.py 让 `TestClient` 默认带上了一条测试会话**
（见那段注释）：默认已登录之后，业务测试不再会撞到 401，于是"认证到底生效没有"
这件事就没有任何用例在看了。这里用**显式不带 cookie 的客户**把它补回来。

两类客户分得很清楚：
    - `shared_db_client`：conftest 那一套（`.tmp/auth-test/auth.db`，里面已经有
      `test-admin`）。用来测"未登录被拒"、"已登录放行"这类**不需要干净账号库**的事。
    - `fresh_db_client`：每个用例一个全新的空账号库（`tmp_path`）。用来测
      初始化、限流、改密码 —— 这些用例要么依赖"账号库是空的"，要么会污染账号状态，
      共用一份账号库就会互相干扰（而且失败时特别难查是谁污染的）。

关于 `monkeypatch.setattr(config, "get_settings", ...)`：
`app.core.auth` / `app.data.users` 这些模块是 `from app.config import get_settings`，
所以**必须打在定义处 `app.config` 上**才有效（打在 `app.core.auth.get_settings` 上
只影响那一个模块，`app.data.users` 里的同名引用还是旧的）。同一条理由见
`conftest.py` 里 `_isolate_llm_env` 的注释。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core.auth import (
    hash_password,
    login_locked_for,
    record_login_failure,
    reset_login_attempts,
    session_cookie_kwargs,
)
from app.data.users import get_auth_store
from app.main import app
from tests.auth_support import TEST_PASSWORD, TEST_USERNAME, mark_client_as_anonymous

#: 未登录时用来撞受保护路由的地址。选 GET `/api/datasets/{id}` 是因为它
#: **不需要请求体** —— 如果认证没拦住，它会回 404（数据集不存在），
#: 而不是回 400/422（缺参数）。这样"401 vs 其它"的差别就纯粹是认证造成的。
PROTECTED_PATH = "/api/datasets/1"

#: PBKDF2-HMAC-SHA256 输出 32 字节 —— 下面会对着 `hash_password` 的真实产物断言一次，
#: 这样"算法换了、盐变短了"之类的事会被立刻发现，而不是等某个用例莫名其妙变红。
_HASH_BYTES = 32
assert len(bytes.fromhex(hash_password("probe-pass1", iterations=1000).hash_hex)) == _HASH_BYTES, (
    "PBKDF2 输出长度变了：检查 app/core/auth.py 的 hashlib.pbkdf2_hmac 调用"
)


def _login_body(username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> dict[str, str]:
    """登录请求体。

    协议是**明文用户名 + 明文密码**（HTTPS 承载），哈希在服务端算 ——
    所以这里只有两个字段。以前"客户端算哈希、只发哈希"的方案已经废弃：
    那样服务端就得给每个账号存一份可离线爆破的等价物，反而更弱。
    """
    return {"username": username, "password": password}


def _session_cookie(client) -> str | None:
    """取客户端手里的会话 cookie 值。

    **不要**用 `client.cookies.get(name)`：httpx 的 `Cookies.get()` 只要发现
    同名 cookie 出现两次就抛 `CookieConflict`，而 Starlette 的 TestClient
    把 base_url 定成 `http://testserver`（域名里没有点），httpx 于是把
    同一个 `Set-Cookie` 既存成 host-only 又存成 `.testserver.local` 一份 ——
    哪怕只有一个会话，`get()` 也会炸。这里直接按"名字 + path=/ + 没标 Domain"
    挑出服务端真正种的那个。
    """
    name = config.get_settings().auth_cookie_name
    for cookie in client.cookies.jar:
        if cookie.name == name and cookie.path == "/":
            return cookie.value
    return None


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_login_limiter():
    """限流状态是进程内的模块级字典，必须逐用例清干净。

    不清的后果很隐蔽：某个用例把 `test-admin` 撞到锁定之后，**下一个**用例
    用同一个用户名登录会拿到 429，看起来像"登录功能坏了"。
    """
    reset_login_attempts()
    yield
    reset_login_attempts()


@pytest.fixture
def shared_db_client():
    """默认客户：conftest 已经把会话塞好了，所以是"已登录"状态。"""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def anonymous_client():
    """同一条会话的客户，但假装浏览器里没有 cookie。

    为什么不能只 `client.cookies.clear()`：conftest 的补丁在**每次请求**前都会
    检查"jar 里有没有会话"，没有就补一条 —— 于是清了又补，"未登录"造不出来。
    所以这里额外用 `mark_client_as_anonymous()` 显式声明"这个 client 就是要匿名"。
    """
    with TestClient(app) as client:
        client.cookies.clear()
        mark_client_as_anonymous(client)
        yield client


@pytest.fixture
def fresh_db_client(tmp_path, monkeypatch):
    """一个**空**账号库 + 一个没有任何 cookie 的客户。

    空账号库是 `/api/auth/bootstrap` 能用起来的唯一条件（库里有账号就 403）。
    """
    db_path = tmp_path / "auth.db"
    settings = config.get_settings().model_copy(update={"auth_db_path": str(db_path)})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    with TestClient(app) as client:
        client.cookies.clear()  # 这个夹具要的是"干净的浏览器"
        mark_client_as_anonymous(client)
        yield client
    reset_auth_store_cache()


def _write_raw_db(db_path, *, username: str, password_hash: str, salt: str, iterations: int = 1000) -> None:
    """绕过 `AuthStore` 直接往库里塞一条"半成品"账号。

    用来测 `iterations` 列 —— `create_user` 收的是调用方算好的值，
    而"某次迁移把旧账号的迭代次数留在了很低的水平"这种情况只能这样复现。

    ⚠️ 表名是 `auth_users`（不是 `users`）：写错表名的报错是
    `sqlite3.OperationalError: no such table: users`，看起来像"库坏了"，
    其实是测试自己拼错了 —— 而 `ensure_schema()` 只会建它自己那两张表。
    """
    from app.data.users import AuthStore

    store = AuthStore(db_path)
    store.ensure_schema()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO auth_users (username, display_name, password_hash, salt,
                                    iterations, created_at, is_active, schema_version)
            VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', 1, 1)
            """,
            (username, username, password_hash, salt, iterations),
        )
        conn.commit()


def _login_body(username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> dict[str, str]:
    """登录请求体 —— **就是用户名 + 明文密码**，与 `LoginRequest` 的字段一一对应。

    这里刻意不"美化"成哈希：真实契约是"密码走 HTTPS 明文提交、服务端加盐哈希后入库"。
    测试如果自己发明一套"客户端先哈希"的字段，测的就不是产品了
    （而且服务端根本不认那些字段，会直接 422）。所以下面有一条用例**专门断言明文不出现在库里**。
    """
    return {"username": username, "password": password}


# ---------------------------------------------------------------------------
# 安全边界：没登录就什么都拿不到
# ---------------------------------------------------------------------------
def test_protected_api_route_rejects_anonymous(anonymous_client):
    """没 cookie 的业务接口必须 401，而且**不能**在响应里泄露任何数据。"""
    response = anonymous_client.get(PROTECTED_PATH)
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_protected_route_rejects_forged_cookie(anonymous_client):
    """随便编一个 token 也不能过（否则"认证"就只剩个装饰）。"""
    anonymous_client.cookies.set(config.get_settings().auth_cookie_name, "x" * 43)
    assert anonymous_client.get(PROTECTED_PATH).status_code == 401


def test_protected_route_rejects_expired_session(anonymous_client, shared_db_client):
    """过期的会话必须被拒 —— 即使它的 token 是服务端真的签发过的。

    做法：拿共享库里那条真实会话的 token，把它的 `expires_at` 直接改成过去。
    这不是"模拟过期"，这就是过期；`resolve_session` 只看库里的那一列。

    ⚠️ 改完必须在 `finally` 里**还原 `expires_at`**：`tests/auth_support.session_token()`
    按库路径缓存 token，整个测试进程共用同一条会话；把它永久改成 2000 年会让后面
    所有"已登录"用例一起 401。实测踩到：单独跑这条绿，跑整套时
    `test_logged_in_client_passes_protected_route`、`test_session_timeout_is_finite` 等一批红。
    """
    shared_db_client.get("/api/auth/me")  # 触发补丁：真实会话在这一刻被种进 jar
    token = _session_cookie(shared_db_client)
    assert token, "conftest 应该给每个 client 补了会话 cookie"

    store = get_auth_store()
    assert store is not None
    from app.core.auth import token_fingerprint

    fingerprint = token_fingerprint(token)
    saved = store.get_session(fingerprint)
    assert saved is not None, "cookie 里的 token 必须在库里，否则这条用例什么都没证明"

    def _set_expiry(value: str) -> None:
        with sqlite3.connect(str(store.path)) as conn:
            conn.execute(
                "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                (value, fingerprint),
            )
            conn.commit()

    _set_expiry("2000-01-01T00:00:00+00:00")
    try:
        # 手动塞一个"真签发过但已过期"的 token：这条用例要的就是"带着过期 cookie 来"。
        anonymous_client.cookies.set(config.get_settings().auth_cookie_name, token)
        assert anonymous_client.get(PROTECTED_PATH).status_code == 401
    finally:
        _set_expiry(saved.expires_at)


def test_logged_in_client_passes_protected_route(shared_db_client):
    """"已登录"这条路径也要正面测一次 —— 否则上面的 401 可能只是"路由坏了"。"""
    response = shared_db_client.get(PROTECTED_PATH)
    assert response.status_code == 404, response.text  # 认证通过，只是数据集 1 不存在
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_get_auth_store_none_when_not_configured(monkeypatch):
    """`AUTH_DB_PATH` 为空 = "这个部署没有账号库"，此时**不能**退化成"不用认证"。

    直接测 `get_auth_store()` 的约定（返回 None），因为"空串变成 `Path('')` = 项目根目录"
    是这段代码当初专门防的坑。路由层把它翻译成 503 这件事由下一个用例覆盖。
    """
    settings = config.get_settings().model_copy(update={"auth_db_path": ""})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    try:
        assert get_auth_store() is None
    finally:
        reset_auth_store_cache()


def test_auth_endpoints_return_503_without_store(anonymous_client, monkeypatch):
    """没配账号库时：带着会话 cookie 访问业务路由回 503（不是 401，也不是放行）。

    为什么要**手动塞一个 cookie**：`resolve_session()` 对"根本没 cookie"的请求
    照旧返回 None → 401（这是对的，用户就是没登录，让他去登录页即可）。
    只有"带了 token、但这个部署压根没接账号库"才是配置故障 —— 那种情况回 401
    会把用户支使去登录，而登录接口本身也在 503，白转一圈。所以两者分开断言。
    """
    settings = config.get_settings().model_copy(update={"auth_db_path": ""})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    try:
        # 没 cookie：普通的未登录，401。
        assert anonymous_client.get(PROTECTED_PATH).status_code == 401

        # 带 cookie：库不可用，必须 503 并且说清是"账号库没启用"。
        anonymous_client.cookies.set(config.get_settings().auth_cookie_name, "whatever-token")
        response = anonymous_client.get(PROTECTED_PATH)
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "账号库" in response.json()["error"]["message"]
    finally:
        reset_auth_store_cache()


def test_login_sets_httponly_samesite_cookie(anonymous_client):
    """会话 cookie 的三件套：HttpOnly + SameSite=Lax + 不 Secure（本机 http 直连）。

    断言的是**登录响应里真实的 Set-Cookie 头**，不是 `session_cookie_kwargs()`
    的返回值 —— 后者只是"打算这么设"，前者才是浏览器真正会遵守的东西。

    `secure=False` 是有意的：本机部署走 http，标了 Secure 浏览器会直接丢掉 cookie，
    用户表现为"登录成功但立刻又是未登录"。这是**已知取舍**，所以写成断言钉住：
    哪天要上 HTTPS，这条用例会红，提醒来这里改。
    """
    response = anonymous_client.post("/api/auth/login", json=_login_body())
    assert response.status_code == 200, response.text

    cookie = response.headers["set-cookie"].lower()
    assert f"{config.get_settings().auth_cookie_name}=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert "secure" not in cookie, "本机 http 部署不能给 cookie 标 Secure，否则浏览器直接丢弃"

    # `session_cookie_kwargs` 是唯一一处决定 cookie 属性的地方，顺手核对它的取值。
    kwargs = session_cookie_kwargs(max_age=1234)
    assert kwargs["httponly"] is True
    assert kwargs["samesite"] == "lax"
    assert kwargs["path"] == "/"
    assert kwargs["secure"] is False
    assert kwargs["max_age"] == 1234


# ---------------------------------------------------------------------------
# 页面路由：/login 公开、/app 要登录
# ---------------------------------------------------------------------------
def test_login_page_is_public(anonymous_client):
    """登录页必须能匿名打开 —— 否则"要登录才能看登录页"是死循环。"""
    response = anonymous_client.get("/login")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")


def test_app_page_redirects_anonymous_to_login(anonymous_client):
    """在地址栏敲 /app：没登录要**跳**登录页，而不是回一屏 401 JSON。

    用户看到的必须是登录页 —— 这是"页面导航"和"API 调用"的区别：
    同一个未认证状态，API 回 401 给前端 fetch 判断，页面回 303 给浏览器跟随。
    """
    response = anonymous_client.get("/app", follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/login"


def test_app_page_serves_index_when_logged_in(shared_db_client):
    response = shared_db_client.get("/app", follow_redirects=False)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")


def test_root_stays_public(anonymous_client):
    """`/` 仍然公开：服务端返回 200 不等于"看得到数据"（数据在 /api/* 后面）。

    这条用例同时是**回归保护** —— 如果以后有人图省事给 `/` 挂上 require_user，
    登录页上的"服务状态"和前端首屏就会 401，而那是没有测试会主动发现的那类改动。
    """
    response = anonymous_client.get("/")
    assert response.status_code == 200, response.text


def test_web_dir_assets_are_reachable(anonymous_client):
    """设计系统的 tokens.css 必须能通过 /static 取到（否则页面样式全丢）。"""
    response = anonymous_client.get("/static/tokens.css")
    assert response.status_code == 200, response.text
    assert "text/css" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# 初始化（bootstrap）
# ---------------------------------------------------------------------------
def test_bootstrap_creates_first_admin_and_logs_in(fresh_db_client):
    """空库 → 创建管理员 → **直接签发会话**（不让用户再输一遍刚设的密码）。"""
    response = fresh_db_client.post(
        "/api/auth/bootstrap",
        json={"username": "alice", "display_name": "Alice", "password": "str0ng-pass"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == "alice"
    assert data["needs_bootstrap"] is False

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()

    # 会话真的生效了：拿它去撞受保护路由，不再是 401。
    assert fresh_db_client.get(PROTECTED_PATH).status_code == 404


def test_bootstrap_rejects_weak_password(fresh_db_client):
    """弱密码在第 7 行就被挡住 —— 库都不该被写。

    错误码用 `app.errors.ErrorCode.BAD_REQUEST` 而不是抄字符串：
    模型校验失败走的是统一错误外壳的 422 分支，码是 BAD_REQUEST
    （`VALIDATION_ERROR` 只是这个项目里**没有**的想象值，抄错就会红）。
    """
    from app.errors import ErrorCode

    response = fresh_db_client.post(
        "/api/auth/bootstrap",
        json={"username": "bob", "display_name": "Bob", "password": "12345678"},
    )
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.BAD_REQUEST.value


def test_bootstrap_rejects_bad_username(fresh_db_client):
    """用户名格式规则只有一份（`app/schemas_auth.py`），这里钉住它确实在生效。"""
    response = fresh_db_client.post(
        "/api/auth/bootstrap",
        json={"username": "-nope", "display_name": "N", "password": "str0ng-pass"},
    )
    assert response.status_code == 422, response.text


def test_bootstrap_forbidden_once_users_exist(shared_db_client):
    """账号库已经有账号 → 403（不能靠这个接口"再建一个管理员"）。"""
    response = shared_db_client.post(
        "/api/auth/bootstrap",
        json={"username": "attacker2", "display_name": "A", "password": "str0ng-pass"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_bootstrap_never_stores_plaintext_password(fresh_db_client):
    """库里的 `password_hash` 不能等于（或包含）明文密码。

    这条是需求里"密码不能明文存储"的直接断言，而且**扫的是磁盘上的字节**，
    不是某个对象的字段 —— 对象字段名改了、列名改了，这条都还能发现。
    """
    password = "un1que-password-xyz"
    response = fresh_db_client.post(
        "/api/auth/bootstrap",
        json={"username": "carol", "display_name": "Carol", "password": password},
    )
    assert response.status_code == 200, response.text

    store = get_auth_store()
    assert store is not None
    raw = store.path.read_bytes()
    assert password.encode() not in raw, "账号库里出现了明文密码"


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def test_login_round_trip_with_real_password(shared_db_client, anonymous_client):
    """真实走一遍登录：拿它签出的 cookie 去访问受保护路由。

    这里登录的是 conftest 建的 `test-admin`（PBKDF2 迭代 1000，所以很快）。
    """
    response = anonymous_client.post(
        "/api/auth/login",
        json=_login_body(),
        headers={"user-agent": "pytest-login-round-trip"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["user"]["username"] == TEST_USERNAME

    token = _session_cookie(anonymous_client)
    assert token, "登录响应应该种下会话 cookie"
    assert len(token) >= 40, "token 太短了，可能是被截断或没走 create_session"

    assert anonymous_client.get(PROTECTED_PATH).status_code == 404  # 认证通过


def test_login_never_stores_plaintext_password(shared_db_client, anonymous_client):
    """登录成功之后，磁盘上的账号库里搜不到明文密码。

    协议走 HTTPS 明文提交、服务端 PBKDF2 加盐哈希，"不落明文"这件事必须
    用**磁盘上的字节**来验证 —— 对象字段名、列名以后改了，这条都还管用。
    """
    response = anonymous_client.post("/api/auth/login", json=_login_body())
    assert response.status_code == 200, response.text

    store = get_auth_store()
    assert store is not None
    raw = store.path.read_bytes()
    assert TEST_PASSWORD.encode() not in raw, "账号库里出现了明文密码"


def test_login_failure_message_does_not_reveal_whether_user_exists(shared_db_client, anonymous_client):
    """用户名不存在 与 密码错误 必须回**同一句话**（否则接口成了用户名枚举机）。"""
    no_such_user = anonymous_client.post(
        "/api/auth/login",
        json=_login_body(username="user-that-does-not-exist", password="whatever1"),
    )
    wrong_password = anonymous_client.post(
        "/api/auth/login",
        json=_login_body(password="wrong-password"),
    )
    assert no_such_user.status_code == 401, no_such_user.text
    assert wrong_password.status_code == 401, wrong_password.text
    assert no_such_user.json()["error"]["message"] == wrong_password.json()["error"]["message"]
    assert "不存在" not in no_such_user.json()["error"]["message"]


def test_login_locks_out_after_repeated_failures(shared_db_client, anonymous_client):
    """连续失败到达阈值后必须限流，并且**带上 Retry-After**（前端要告诉用户等多久）。

    这是**HTTP 级**用例（真实 POST 登录接口），不是"直接调限流函数"的单元路径 ——
    两者差别很关键：`login_locked_for()` 如果在每次请求开头把"错了但还没锁定"的
    计数当成过期状态清掉，单元路径照样绿，只有真实请求序列会暴露"永远锁不上"。
    """
    settings = config.get_settings()
    payload = _login_body(password="wrong-password")

    for _ in range(settings.auth_max_failed_attempts):
        assert anonymous_client.post("/api/auth/login", json=payload).status_code == 401

    assert login_locked_for(TEST_USERNAME) > 0, "达到阈值后应该已经锁定"

    locked = anonymous_client.post("/api/auth/login", json=payload)
    assert locked.status_code == 429, locked.text
    assert locked.json()["error"]["code"] == "RATE_LIMITED"
    assert int(locked.headers["retry-after"]) >= 1


def test_login_lockout_does_not_run_pbkdf2(shared_db_client, anonymous_client, monkeypatch):
    """被锁定之后再撞，**不许**再跑一次密码校验。

    "先查限流、再验密码"这个顺序的价值就在这里：反过来的话，一个被锁的账号
    仍然能让服务端免费跑 PBKDF2（产品默认 240000 次迭代），等于送给攻击者一个
    CPU 耗尽的入口。做法：把 `authenticate` 换成"被调用就翻牌子"的桩，
    并且这次提交**正确的密码** —— 只要响应还是 429、桩没被调用，
    就说明被挡住的原因是限流，而不是密码不对。
    """
    from app.core import auth as auth_core

    calls = []
    monkeypatch.setattr(auth_core, "authenticate", lambda *a, **k: calls.append(a) or None)

    settings = config.get_settings()
    for _ in range(settings.auth_max_failed_attempts):
        record_login_failure(TEST_USERNAME)

    response = anonymous_client.post("/api/auth/login", json=_login_body())
    assert response.status_code == 429, response.text
    assert calls == [], "锁定状态下仍然调用了 authenticate()"


def test_login_accepts_lower_iteration_account(tmp_path, monkeypatch):
    """兼容老账号：库里的 `iterations` 低于当前默认值时，仍然能登进来。

    PBKDF2 的迭代次数是**存在账号行上**的，所以升级默认值不会把老用户锁在门外。
    这条用例守的就是这个兼容性 —— 有人"顺手"改成读全局配置（那样老账号就永远
    验不过）就会红。
    """
    db_path = tmp_path / "legacy.db"
    # 先按低迭代算一份，再手写进库里（create_user 会原样收下，但这里要显式表达"旧数据"）。
    hashed = hash_password("legacy-pass1", iterations=1000)
    _write_raw_db(db_path, username="legacy", password_hash=hashed.hash_hex, salt=hashed.salt_hex)

    settings = config.get_settings().model_copy(update={"auth_db_path": str(db_path)})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    try:
        with TestClient(app) as client:
            client.cookies.clear()
            response = client.post(
                "/api/auth/login",
                json={"username": "legacy", "password": "legacy-pass1"},
            )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["user"]["username"] == "legacy"
    finally:
        reset_auth_store_cache()


def test_login_response_hides_password_material(shared_db_client, anonymous_client):
    """响应体里不能出现 password_hash / salt / iterations 任何一个字段名。

    需求里"密码材料绝不回传"这条，用**序列化后的字符串**来断言：
    以后有人给 `UserData` 加一个字段，这条会立刻红。
    """
    text = anonymous_client.post("/api/auth/login", json=_login_body()).text
    for forbidden in ("password_hash", "salt", "iterations", "password"):
        assert forbidden not in text, f"登录响应泄露了 {forbidden}"


# ---------------------------------------------------------------------------
# 当前登录状态 /auth/me
# ---------------------------------------------------------------------------
def test_auth_me_anonymous_reports_not_authenticated(anonymous_client):
    """公开接口：匿名访问回 `authenticated=false`，而不是 401。"""
    body = anonymous_client.get("/api/auth/me").json()
    assert body["ok"] is True
    assert body["data"]["authenticated"] is False
    assert body["data"]["user"] is None


def test_auth_me_authenticated_reports_user_and_expiry(shared_db_client):
    body = shared_db_client.get("/api/auth/me").json()
    data = body["data"]
    assert data["authenticated"] is True
    assert data["user"]["username"] == TEST_USERNAME
    assert data["session_expires_at"], "已登录时应给出会话到期时间"


# ---------------------------------------------------------------------------
# 改密码
# ---------------------------------------------------------------------------
def _bootstrap_and_login(client, *, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/bootstrap",
        json={"username": username, "display_name": username, "password": password},
    )
    assert response.status_code == 200, response.text


def test_change_password_requires_current_password(fresh_db_client):
    """只报新密码不给旧密码 → 模型层就挡住（422）。"""
    _bootstrap_and_login(fresh_db_client, username="dave", password="first-pass1")
    response = fresh_db_client.post("/api/auth/password", json={"new_password": "second-pass2"})
    assert response.status_code == 422, response.text


def test_change_password_rejects_wrong_current_password(fresh_db_client):
    _bootstrap_and_login(fresh_db_client, username="erin", password="first-pass1")
    response = fresh_db_client.post(
        "/api/auth/password",
        json={"current_password": "not-the-password", "new_password": "second-pass2"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["message"] == "当前密码不正确"


def test_change_password_keeps_current_device_logged_in(fresh_db_client):
    """改完密码**当前设备**不能掉线（否则体感像"改密码失败了"）。"""
    _bootstrap_and_login(fresh_db_client, username="frank", password="first-pass1")
    response = fresh_db_client.post(
        "/api/auth/password",
        json={"current_password": "first-pass1", "new_password": "second-pass2"},
    )
    assert response.status_code == 200, response.text
    assert fresh_db_client.get("/api/auth/me").json()["data"]["authenticated"] is True
    assert fresh_db_client.get(PROTECTED_PATH).status_code == 404  # 认证通过


def test_change_password_revokes_other_devices(fresh_db_client):
    """改密码要踢掉**别的**设备的会话 —— 这是"改密码"这个动作的核心承诺。

    两个客户 = 两台设备（同一条账号库，两条独立会话）。
    """
    _bootstrap_and_login(fresh_db_client, username="grace", password="first-pass1")

    # 第二台设备：重新登录一次，拿一条自己的会话。
    with TestClient(app) as other_device:
        other_device.cookies.clear()
        mark_client_as_anonymous(other_device)  # 别让 conftest 的补丁替它"已登录"
        login = other_device.post(
            "/api/auth/login",
            json={"username": "grace", "password": "first-pass1"},
        )
        assert login.status_code == 200, login.text
        assert other_device.get("/api/auth/me").json()["data"]["authenticated"] is True

        # 第一台设备改密码。
        changed = fresh_db_client.post(
            "/api/auth/password",
            json={"current_password": "first-pass1", "new_password": "second-pass2"},
        )
        assert changed.status_code == 200, changed.text

        # 第二台设备的会话必须已经作废。
        assert other_device.get("/api/auth/me").json()["data"]["authenticated"] is False
        assert other_device.get(PROTECTED_PATH).status_code == 401


def test_change_password_updates_login_credential(fresh_db_client):
    """改完之后：新密码能登、旧密码不能登。"""
    _bootstrap_and_login(fresh_db_client, username="heidi", password="first-pass1")
    fresh_db_client.post(
        "/api/auth/password",
        json={"current_password": "first-pass1", "new_password": "second-pass2"},
    )

    with TestClient(app) as probe:
        probe.cookies.clear()
        mark_client_as_anonymous(probe)
        assert probe.post("/api/auth/login", json={"username": "heidi", "password": "first-pass1"}).status_code == 401
        ok = probe.post("/api/auth/login", json={"username": "heidi", "password": "second-pass2"})
        assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# 会话列表 / 退出
# ---------------------------------------------------------------------------
def test_sessions_list_marks_current_and_hides_tokens(shared_db_client):
    """"我的登录设备"要能看出哪条是当前会话，但**绝不能**把 token 给出去。"""
    response = shared_db_client.get("/api/auth/sessions")
    assert response.status_code == 200, response.text
    sessions = response.json()["data"]
    assert isinstance(sessions, list)
    assert any(item["current"] for item in sessions), "至少要有一条被标成当前会话"

    text = response.text
    token = _session_cookie(shared_db_client)
    assert token not in text
    for forbidden in ("token_hash", "token"):
        assert forbidden not in text


def test_logout_destroys_session_server_side(anonymous_client):
    """退出要**服务端销毁**，不能只清浏览器的 cookie。

    做法：给测试账号签一条**本条用例专用**的会话，带着它退出，然后把同一个
    token 手动塞回一个干净的客户 —— 如果服务端只是让浏览器忘掉它，那么这个
    "捡到的 token"还能用（需求里"退出后会话必须失效"直接判红）。

    为什么自己签一条而不用 conftest 那条共享会话：退出会把 token 从库里删掉，
    而 `tests/auth_support.session_token()` 的 token 是**整个测试进程共用**的
    （按库路径缓存）。删了共享 token，后面每个"已登录"用例都会 401。
    """
    from app.core.auth import create_session
    from app.data.users import get_auth_store

    store = get_auth_store()
    assert store is not None
    from tests.auth_support import ensure_test_user

    user_id = ensure_test_user(store)
    token, _expires = create_session(user_id, user_agent="pytest-logout-probe")

    assert anonymous_client.get(PROTECTED_PATH).status_code == 401  # 先确认它本来没登录
    anonymous_client.cookies.set(config.get_settings().auth_cookie_name, token)
    assert anonymous_client.get(PROTECTED_PATH).status_code == 404  # 这条 token 是有效的

    assert anonymous_client.post("/api/auth/logout").status_code == 200

    # 退出之后同一个 token 必须作废 —— 换成另一个干净客户再试一次。
    with TestClient(app) as reused:
        reused.cookies.clear()
        mark_client_as_anonymous(reused)
        reused.cookies.set(config.get_settings().auth_cookie_name, token)
        assert reused.get(PROTECTED_PATH).status_code == 401


def test_logout_is_idempotent_for_anonymous(anonymous_client):
    """没登录也能退出，而且不报错 —— 否则前端要处理"退出失败"这个不该存在的状态。"""
    response = anonymous_client.post("/api/auth/logout")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_logout_clears_cookie_header(anonymous_client):
    """响应里要带一个"把 cookie 清掉"的 Set-Cookie（否则浏览器里它还留着）。"""
    response = anonymous_client.post("/api/auth/logout")
    cookie = response.headers.get("set-cookie", "")
    assert config.get_settings().auth_cookie_name in cookie
    assert "Max-Age=0" in cookie or "max-age=0" in cookie


# ---------------------------------------------------------------------------
# 跨账号隔离
# ---------------------------------------------------------------------------
def test_session_does_not_grant_other_accounts_data(tmp_path, monkeypatch):
    """会话只认 token，不认"这个浏览器之前是谁"。

    两个账号 A/B 各自一条会话，B 的 token **不能**让 A 的客户端继续工作：
    这里把 A 客户端的 cookie 换成 B 的 token，`/api/auth/me` 必须回答 B。
    """
    db_path = tmp_path / "two-users.db"
    settings = config.get_settings().model_copy(update={"auth_db_path": str(db_path)})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    try:
        with TestClient(app) as alice:
            alice.cookies.clear()
            mark_client_as_anonymous(alice)  # 空库才能 bootstrap，别被补丁会话挡住
            assert alice.post(
                "/api/auth/bootstrap",
                json={"username": "alice", "display_name": "Alice", "password": "alice-pass1"},
            ).status_code == 200
            assert alice.post("/api/auth/logout").status_code == 200

            with TestClient(app) as bob:
                bob.cookies.clear()
                mark_client_as_anonymous(bob)
                assert bob.post(
                    "/api/auth/bootstrap",
                    json={"username": "bob", "display_name": "Bob", "password": "bob-pass1"},
                ).status_code == 403  # 库不空了，只能走登录
                # 直接给 bob 建账号（模拟"已经有两个账号"的部署）。
                from app.core.auth import create_session, hash_password

                store = get_auth_store()
                assert store is not None
                store.ensure_schema()
                hashed = hash_password("bob-pass1", iterations=1000)
                record = store.create_user(
                    username="bob",
                    display_name="Bob",
                    password_hash=hashed.hash_hex,
                    salt=hashed.salt_hex,
                    iterations=hashed.iterations,
                )
                bob_token, _ = create_session(record.id)

                alice.cookies.set(config.get_settings().auth_cookie_name, bob_token)
                me = alice.get("/api/auth/me").json()["data"]
                assert me["authenticated"] is True
                assert me["user"]["username"] == "bob", "换一个 token 必须换一个身份"
    finally:
        reset_auth_store_cache()


def test_session_timeout_is_finite(shared_db_client):
    """会话必须有明确的有效期（"永不过期的登录"= 一条 cookie 永久有效）。"""
    settings = config.get_settings()
    assert settings.auth_session_seconds > 0
    assert settings.auth_session_seconds <= 60 * 60 * 24 * 30, "会话有效期长到不像话（>30 天）"

    body = shared_db_client.get("/api/auth/me").json()["data"]
    expires_at = body["session_expires_at"]
    assert expires_at
    from datetime import datetime

    deadline = datetime.fromisoformat(expires_at)
    assert deadline.timestamp() > time.time(), "会话到期时间应该在未来"


# ---------------------------------------------------------------------------
# 演示账号一键登录（/api/auth/demo）
#
# 这个接口的性质和别的都不一样：它**故意**不需要密码。所以测试要盯的不是
# "能不能登进去"，而是"凭什么能被允许存在"：
#   - 能关掉（DEMO_LOGIN_ENABLED=0 时 403，且前端能知道按钮不该出现）；
#   - 空账号库时拒绝（不许抢走首次部署的位置）；
#   - 同名真实账号不被顶替（不能把别人的账号变成演示账号）；
#   - 开着的时候真的建号 + 发会话 + 幂等，而演示账号密码是随机的、猜不到。
# ---------------------------------------------------------------------------
def _demo_settings(tmp_path, monkeypatch, *, db_name: str = "auth-demo.db", **extra):
    """把账号库指到 tmp_path 并设定演示登录开关。返回值只用于断言。"""
    db_path = tmp_path / db_name
    settings = config.get_settings().model_copy(update={"auth_db_path": str(db_path), **extra})
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from app.data.users import reset_auth_store_cache

    reset_auth_store_cache()
    return settings


def test_demo_login_can_be_turned_off(tmp_path, monkeypatch):
    """关掉开关后接口必须 403，并且 /auth/me 明确告诉前端"别显示按钮"。

    这是整条防线的第一层：前端按钮显不显示只是结果，后端认的是配置。
    """
    _demo_settings(tmp_path, monkeypatch, demo_login_enabled=False)
    try:
        with TestClient(app) as client:
            client.cookies.clear()
            mark_client_as_anonymous(client)

            body = client.get("/api/auth/me").json()["data"]
            assert body["demo_login"] is False

            response = client.post("/api/auth/demo")
            assert response.status_code == 403
            error = response.json()["error"]
            assert error["code"] == "FORBIDDEN"
            assert "DEMO_LOGIN_ENABLED" in error["message"]
    finally:
        from app.data.users import reset_auth_store_cache

        reset_auth_store_cache()


def test_demo_login_refuses_to_steal_bootstrap(tmp_path, monkeypatch):
    """空账号库时演示登录必须拒绝：否则它会占掉"第一个账号"而关死初始化入口。"""
    _demo_settings(tmp_path, monkeypatch, demo_login_enabled=True)
    try:
        with TestClient(app) as client:
            client.cookies.clear()
            mark_client_as_anonymous(client)
            response = client.post("/api/auth/demo")
            assert response.status_code == 403
            assert "首次部署" in response.json()["error"]["message"]
            # 仍然可以正常初始化 —— 演示登录没有把库弄脏。
            assert client.post(
                "/api/auth/bootstrap",
                json={"username": "admin", "display_name": "管理员", "password": "admin-pass1"},
            ).status_code == 200
    finally:
        from app.data.users import reset_auth_store_cache

        reset_auth_store_cache()


def test_demo_login_creates_account_issues_session_and_is_idempotent(tmp_path, monkeypatch):
    """打开开关后：没账号就建一个演示账号、签发会话、再点一次还是同一个账号。"""
    settings = _demo_settings(tmp_path, monkeypatch, demo_login_enabled=True)
    try:
        # 先按正常流程建一个管理员（演示登录自己拒绝在空库上工作）。
        with TestClient(app) as admin:
            admin.cookies.clear()
            mark_client_as_anonymous(admin)
            assert admin.post(
                "/api/auth/bootstrap",
                json={"username": "admin", "display_name": "管理员", "password": "admin-pass1"},
            ).status_code == 200

        with TestClient(app) as client:
            client.cookies.clear()
            mark_client_as_anonymous(client)

            # 还没点按钮时，/auth/me 已经告诉前端"这个部署可以演示登录"。
            state = client.get("/api/auth/me").json()["data"]
            assert state["authenticated"] is False
            assert state["needs_bootstrap"] is False
            assert state["demo_login"] is True

            response = client.post("/api/auth/demo")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["authenticated"] is True
            assert data["demo_login"] is True
            assert data["user"]["username"] == settings.demo_username
            assert data["user"]["display_name"] == "演示账号"
            assert _session_cookie(client), "演示登录也要真的种会话 cookie"

            # 会话真的能用（不是只返回了一个好看的响应体）。
            me = client.get("/api/auth/me").json()["data"]
            assert me["authenticated"] is True
            assert me["user"]["username"] == settings.demo_username

            # 幂等：第二次点不会建出第二个演示账号。
            again = client.post("/api/auth/demo").json()["data"]
            assert again["user"]["id"] == data["user"]["id"]

            # 演示账号的密码是服务端随机丢掉的，拿常用密码从登录表单进不来。
            with TestClient(app) as guesser:
                guesser.cookies.clear()
                mark_client_as_anonymous(guesser)
                wrong = guesser.post(
                    "/api/auth/login",
                    json={"username": settings.demo_username, "password": "demo1234"},
                )
                assert wrong.status_code == 401
    finally:
        from app.data.users import reset_auth_store_cache

        reset_auth_store_cache()


def test_demo_login_never_impersonates_a_real_account(tmp_path, monkeypatch):
    """同名真实账号不能被演示登录顶替：接口拒绝，且原账号密码仍然有效。"""
    _demo_settings(tmp_path, monkeypatch, demo_login_enabled=True)
    try:
        with TestClient(app) as client:
            client.cookies.clear()
            mark_client_as_anonymous(client)
            # 这个部署的真实管理员恰好就叫 demo，显示名不是「演示账号」。
            assert client.post(
                "/api/auth/bootstrap",
                json={"username": "demo", "display_name": "真正的管理员", "password": "real-pass1"},
            ).status_code == 200
            assert client.post("/api/auth/logout").status_code == 200

            blocked = client.post("/api/auth/demo")
            assert blocked.status_code == 403
            assert "占用" in blocked.json()["error"]["message"]

            # 账号没有被改成演示账号：原密码照样能登录。
            ok = client.post("/api/auth/login", json={"username": "demo", "password": "real-pass1"})
            assert ok.status_code == 200
            assert ok.json()["data"]["user"]["display_name"] == "真正的管理员"
    finally:
        from app.data.users import reset_auth_store_cache

        reset_auth_store_cache()
