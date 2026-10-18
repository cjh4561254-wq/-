"""测试专用：给 `TestClient` 一个**真实会话**，而不是绕过认证。

为什么需要它
------------
2026-10-16 的登录功能给三个业务 router 挂上了 `Depends(require_user)`。
测试里 30 多处 `TestClient(app)` 没有 cookie，于是全部拿到 401 ——
这是**被测行为正确、测试没跟上**，不是回归。

任务书禁止"删测试 / skip / xfail / 把断言改宽"来换绿色。所以这里做的事是：
在测试的账号库里真的建一个账号、真的签发一条会话、把这条会话真的塞进客户的
cookie jar 里。测试走的仍然是完整的认证链路（`require_user` → `resolve_session`
→ 查库 → 校验过期），只是"登录"这一步不用 HTTP 走一遍。

为什么不用 HTTP 调一次 `/api/auth/bootstrap`
-------------------------------------------
两个原因：
1. **顺序反了**。测试的用法是"先 `with TestClient(app)`，进上下文时才跑 lifespan"；
   而 cookie 必须在请求之前就在 jar 里。用 HTTP 登录就得先起第二个 client、
   跑一遍 lifespan，等于每个用例多起一次应用。
2. **PBKDF2 太慢**。真实迭代次数 240000（约 0.1 秒/次），而这里要给几十个用例
   各签一条会话 —— 那不是"测认证"，那是给测试套件加几十秒。

所以这里直接用 `app/core/auth.py` 的公开函数（`hash_password` / `create_session`），
迭代次数压到 1000（`_TEST_ITERATIONS`）。注意：**产品默认值仍然是 240000**，
有专门的用例断言那个下限（`tests/test_auth_api.py`）。
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.core.auth import create_session, hash_password
from app.data.users import AuthStore, AuthStoreError, get_auth_store

#: 测试账号的固定凭据。故意用弱密码 —— 它只存在于 `.tmp/` 里的临时库，
#: 而且"弱密码能不能登录"不是这些用例要测的东西。
TEST_USERNAME = "test-admin"
TEST_PASSWORD = "test-admin-password"

#: 测试用的 PBKDF2 迭代次数。1000 是 `Settings.auth_pbkdf2_iterations` 的
#: 合法下限（`ge=1000`），比 240000 快两个数量级。产品默认值不受影响。
_TEST_ITERATIONS = 1000

#: 把 token 缓存起来：同一个账号库不必给每个用例都重算一次 PBKDF2。
#: key 是账号库文件路径 —— 配置被用例改过（例如换到 `tmp_path`）时会自动失效。
_SESSION_CACHE: dict[str, str] = {}


def ensure_test_user(store: AuthStore | None = None) -> int:
    """保证测试账号库里有一个可用账号，返回它的 id。

    幂等：已经有同名账号就直接用（用户名查找是大小写不敏感的）。
    """
    if store is None:
        store = get_auth_store()
    if store is None:
        raise RuntimeError(
            "账号库没有启用（AUTH_DB_PATH 为空）。测试环境应当由 conftest.py "
            "把 AUTH_DB_PATH 指到 .tmp/auth-test/auth.db —— 检查它是不是被别的用例清掉了。"
        )

    store.ensure_schema()
    existing = store.find_user(TEST_USERNAME)
    if existing is not None:
        return existing.id

    hashed = hash_password(TEST_PASSWORD, iterations=_TEST_ITERATIONS)
    try:
        user = store.create_user(
            username=TEST_USERNAME,
            display_name="测试管理员",
            password_hash=hashed.hash_hex,
            salt=hashed.salt_hex,
            iterations=hashed.iterations,
        )
    except AuthStoreError:
        # 并发/重复调用时另一个来源刚好建好了 —— 直接用那条。
        existing = store.find_user(TEST_USERNAME)
        if existing is None:
            raise
        return existing.id
    return user.id


def session_token() -> str:
    """给测试账号签一条**真实会话**，返回 token 明文（带缓存）。

    token 明文只在签发时存在一次（库里存的是 SHA-256），所以缓存的是明文本身。
    """
    store = get_auth_store()
    if store is None:
        raise RuntimeError("账号库没有启用（AUTH_DB_PATH 为空），无法签发测试会话")

    key = str(store.path)
    cached = _SESSION_CACHE.get(key)
    if cached is not None:
        return cached

    user_id = ensure_test_user(store)
    token, _expires = create_session(user_id, user_agent="pytest")
    _SESSION_CACHE[key] = token
    return token


def default_auth_cookies(base_url: str = "http://testserver") -> dict[str, str]:
    """`{cookie 名: token}`，可以直接喂给 `TestClient(app, cookies=...)`。"""
    from app.config import get_settings

    return {get_settings().auth_cookie_name: session_token()}


def authenticated_client(app, **kwargs) -> TestClient:
    """显式拿一个"已登录"的 client（需要自己控制 cookie 时用这个）。

    `TestClient(app)` 默认就会带上会话（见 conftest.py 的 `_auto_login_test_clients`），
    这个函数存在的意义是"让'我要一个已登录的 client'这句话在测试里可见"。
    """
    kwargs.setdefault("cookies", default_auth_cookies())
    return TestClient(app, **kwargs)


def attach_session_cookie(client: TestClient) -> TestClient:
    """把测试会话塞进这个 client 的 cookie jar（已经有了就不覆盖）。

    不覆盖是有意的：用例可以先用 `client.cookies.set(名字, "坏 token")` 造一个
    "会话无效"的场景，这里的补丁不该把它冲掉。

    ⚠️ 这里**故意不传 `domain=`**（曾经传过 `domain="testserver"`，是错的）。
    httpx 的 `Cookies.set(name, value, domain=...)` 会把 domain 当成"调用方显式指定"，
    种出一条 `Domain=testserver` 的 cookie；之后 httpx 发请求时按 RFC 6265 的域匹配
    去查 jar —— `testserver` 不是 `testserver` 的有效 cookie 域（没有前导点、也不相等），
    于是**这条 cookie 永远不会被发出去**。表现极具误导性：`cookie in client.cookies`
    是 True、`resolve_session(token)` 单独调也没问题，但请求到服务端就是 401。
    不传 domain，httpx 就会把它当成 host-only cookie 绑到当前请求的 host 上，
    请求时自然带上。
    """
    from app.config import get_settings

    name = get_settings().auth_cookie_name
    if name not in client.cookies:
        client.cookies.set(name, session_token(), path="/")
    return client


def mark_client_as_anonymous(client: TestClient) -> TestClient:
    """把这个 client 标记成"就是没登录"，`attach_session_cookie` 不再给它补会话。

    为什么需要这个开关：conftest 的补丁在**每次请求**前都会检查"jar 里有没有会话"，
    没有就补一条。于是 `client.cookies.clear()` 是**没用的** —— 清掉之后下一次请求
    又被补上，"未登录"这个场景根本造不出来（实测：`anonymous_client` 清空 cookie 后
    仍然带着会话，受保护接口回 404 而不是 401，用例红得莫名其妙）。
    用实例属性（而不是类属性/全局标志）是为了让"我要匿名"只影响这一个 client，
    同一条用例里另一个"已登录"的 client 照常工作。
    """
    client._dsh_no_auto_session = True  # type: ignore[attr-defined]
    return client
