"""真实认证的核心：密码哈希、会话签发与校验、登录限流、受保护路由依赖。

这个模块是整个登录功能的"大脑"，`app/api/routes_auth.py` 只做 HTTP 翻译，
`app/data/users.py` 只做持久化。三条边界不能互相越界。

**零新依赖**：`hashlib` / `hmac` / `secrets` / `time` 全是标准库。
不引 passlib、不引 bcrypt、不引 python-jose —— 它们能做的一切，
标准库里都有等价物，而这个项目的纪律是"能为几十行代码不引依赖就不引"
（与 `app/data/history.py` 拒绝 SQLAlchemy 是同一条理由）。

密码为什么用 PBKDF2-HMAC-SHA256
------------------------------
密码的熵很低（"123456"、"password2024" 都在真实泄露库里出现过几百万次），
所以密码哈希必须**故意慢**，让离线暴力破解的每一次尝试都要付出代价。

- `hashlib.pbkdf2_hmac("sha256", ...)` 是标准库里的成熟实现（RFC 8018），
  迭代次数写在库里、跟着账号走，所以以后调高不会让老账号登不进来。
- **每个账号一个独立的随机盐**（`secrets.token_bytes(16)`）。没有盐的话，
  两个用同样密码的人会有同样的哈希值，一张彩虹表就能同时破掉一批人；
  更糟的是"这个哈希值我见过"本身就在泄露信息。盐不需要保密，但必须唯一。
- **不用 SHA-256 直接哈希密码**。那比 PBKDF2 快几十万倍，
  等于把"暴力破解要几年"变成"要几秒"。

会话 token 为什么是一次 SHA-256，而不是也上 PBKDF2
-----------------------------------------------
两者的威胁模型不一样：
- 密码：熵低、会被人猜、会被拖库，所以必须抗暴力破解 → 慢哈希 + 盐。
- 会话 token：`secrets.token_urlsafe(32)` 生成的 256 位**真随机**值，
  猜不出来、也不需要"抗暴力破解" → 只要不在库里存明文就够了。
  存 `sha256(token)` 的意义是：库文件被拷走也无法冒充已登录用户。

登录失败为什么要"同一句话"、还要"假装查一下"
------------------------------------------
两件事都能被用来**枚举用户名**：
1. 提示文案区分"用户名不存在"和"密码错误" → 攻击者能筛出哪些用户名真实存在。
   所以两种情况一律回"用户名或密码不正确"。
2. 用户不存在时直接返回、存在时才跑 PBKDF2 → 两种情况的**响应时间**差一个数量级。
   所以用户不存在时也跑一次同样代价的 PBKDF2（拿一个固定的假盐），
   让时间看起来一样。

这两条都不是"过度设计"：登录接口是唯一一个**匿名可访问**的写接口，
它天然是攻击面第一个被摸的地方。

会话为什么是固定过期时间，不做滑动续期
------------------------------------
`touch_session()` 只更新 `last_seen_at`（给人看的"最近活跃"），
**不延长 `expires_at`**。滑动续期看起来更友好，但它意味着"只要一直点，
会话就永不过期"—— 一个被偷走的 token 可以被无限期使用。
固定 8 小时的上限是可以对用户说清楚的，也可以被审计。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request

from app.config import get_settings
from app.data.users import AuthStoreError, UserRecord, get_auth_store
from app.errors import AppError
from app.schemas import ErrorCode

#: 盐的字节数。16 字节 = 128 位，是 RFC 8018 的推荐下限。
_SALT_BYTES = 16

#: 会话 token 的随机字节数。32 字节 = 256 位。
#: `token_urlsafe(32)` 会编码成 43 个字符 —— 这个长度本身就说明它不可能被猜中。
_TOKEN_BYTES = 32

#: 用户不存在时用来"假装查一下"的固定盐。
#: 它的值不重要（反正算出来的结果一定对不上），重要的是**代价**与真实校验一致。
_DUMMY_SALT = bytes.fromhex("00" * _SALT_BYTES)


@dataclass(frozen=True)
class PasswordHash:
    """一次哈希的全部产物。三个值都要存进库 —— 少一个就再也验不回来。"""

    hash_hex: str
    salt_hex: str
    iterations: int


@dataclass(frozen=True)
class SessionUser:
    """当前登录用户的最小身份信息。

    故意**不带** `password_hash` / `salt`：这个对象会被注入到业务路由里，
    它没有理由拿到校验材料。字段越少，越不可能被意外序列化进响应。
    """

    id: int
    username: str
    display_name: str


def hash_password(password: str, *, iterations: int | None = None) -> PasswordHash:
    """把一个明文密码变成可入库的三元组。

    `iterations` 一般不传（用配置里的默认值）；测试传小值以跑得快 ——
    注意测试里"迭代次数少"不代表产品里也少，产品用的是配置默认值，
    并有专门的用例断言那个默认值**不低于** 240000。
    """
    settings = get_settings()
    rounds = iterations if iterations is not None else settings.auth_pbkdf2_iterations
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return PasswordHash(
        hash_hex=digest.hex(),
        salt_hex=salt.hex(),
        iterations=rounds,
    )


def verify_password(
    password: str,
    *,
    hash_hex: str,
    salt_hex: str,
    iterations: int,
) -> bool:
    """校验密码。用 `hmac.compare_digest` 而不是 `==`。

    为什么不是 `==`：Python 的字符串比较**短路**——第一个不同的字符就返回，
    所以"前缀猜对了几位"会让比较耗时稍长一点点。理论上可以据此一位一位地
    试出哈希值。`compare_digest` 的耗时与内容无关，把这个信道堵死。

    任何解析失败（盐不是合法 hex、迭代次数是 0）都返回 `False` 而不是抛异常：
    一条坏掉的账号记录不应该让登录接口 500，只应该让那个人登不进来。
    """
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    if not salt or iterations <= 0:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    try:
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected)


def _burn_password_time(password: str) -> None:
    """用户不存在时也花掉一次真实的 PBKDF2 时间（防用户名枚举）。

    不做这件事的话，"用户名不存在"会比"密码错误"快一个数量级，
    攻击者据此就能筛出哪些用户名是真的。
    """
    settings = get_settings()
    hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        _DUMMY_SALT,
        settings.auth_pbkdf2_iterations,
    )


def new_session_token() -> str:
    """一条新的会话 token。`token_urlsafe` 用的是 `os.urandom`，密码学安全。"""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """token 的 SHA-256（十六进制）。库里只存这个，不存 token 本身。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 登录限流（进程内存，按用户名）
# ---------------------------------------------------------------------------
# 为什么不落库：限流状态是**短命**的（5 分钟窗口），重启清空完全可接受；
# 而写库会给"用不存在的用户名疯狂尝试"提供一个免费的写放大攻击面
# —— 每次失败都写一行，反而把库撑爆。
#
# 为什么不按 IP：本机开发场景下所有请求都来自 127.0.0.1，按 IP 等于把
# 所有人算成同一个人，第一个输错密码的人会锁住全部人。
@dataclass
class _AttemptState:
    failures: int
    locked_until: float


_attempts: dict[str, _AttemptState] = {}
_attempts_lock = threading.Lock()


def _attempt_key(username: str) -> str:
    return username.strip().lower()


def login_locked_for(username: str) -> int:
    """返回还要锁多少秒；没被锁返回 0。

    ⚠️ 这个函数**不能顺手清掉"还没锁定"的计数**。路由在每次登录请求开头都会调它，
    如果把 `failures=1, locked_until=0.0` 这种"错了但还没到阈值"的状态也当成
    "锁已过期"删掉，失败计数就会在第 2 次请求进来时被清零 → `failures` 永远停在 1
    → 阈值永远够不到 → 限流形同虚设（真实 HTTP 路径实测：连续 6 次错误密码全是 401，
    没有 429）。只有**真的锁过期**（`locked_until` 曾经设过，且已经过去）才清理，
    顺便防止字典无限增长。

    区分两种 remaining <= 0：
    - `locked_until == 0`：从没锁过 —— 返回 0，但保留计数继续累加；
    - `locked_until > 0` 且已过去：锁过期 —— 删状态、返回 0。
    """
    now = time.monotonic()
    with _attempts_lock:
        key = _attempt_key(username)
        state = _attempts.get(key)
        if state is None:
            return 0
        remaining = state.locked_until - now
        if remaining > 0:
            return int(remaining) + 1  # 向上取整，避免显示"还要等 0 秒"
        if state.locked_until > 0.0:
            # 锁真的过期了：清掉状态（等价于"解锁后重新给满额次数"）
            _attempts.pop(key, None)
        # 从未锁定：保留 failures，让下次 record_login_failure 继续累加
        return 0


def record_login_failure(username: str) -> None:
    settings = get_settings()
    key = _attempt_key(username)
    with _attempts_lock:
        state = _attempts.get(key)
        if state is None:
            state = _AttemptState(failures=0, locked_until=0.0)
            _attempts[key] = state
        state.failures += 1
        if state.failures >= settings.auth_max_failed_attempts:
            state.locked_until = time.monotonic() + settings.auth_lockout_seconds
            # 锁上之后计数归零：解锁后重新给满额次数，
            # 否则一个 5 年前错过的用户会被"永久累加"到锁死。
            state.failures = 0


def record_login_success(username: str) -> None:
    with _attempts_lock:
        _attempts.pop(_attempt_key(username), None)


def reset_login_attempts() -> None:
    """清空全部限流状态。测试用（也用于"运维手动解锁"这种场景）。"""
    with _attempts_lock:
        _attempts.clear()


# ---------------------------------------------------------------------------
# 登录 / 登出 / 会话解析
# ---------------------------------------------------------------------------
class LoginFailure(Exception):
    """登录失败。`reason` 是给**服务端日志**看的，不是给用户看的。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require_store():
    """取账号库；没配就抛 503。

    文案不写"无法登录"，因为这个函数现在也被 `resolve_session()` 用 ——
    那时的场景是"带着会话 cookie 访问业务接口，但账号库没启用"，
    说"无法登录"会让用户以为要再登一次（而不是去查 AUTH_DB_PATH）。
    """
    store = get_auth_store()
    if store is None:
        raise AppError(
            ErrorCode.SERVICE_UNAVAILABLE,
            "账号库没有启用，无法校验登录状态（检查 AUTH_DB_PATH 配置）",
        )
    return store


def authenticate(username: str, password: str) -> UserRecord:
    """校验用户名 + 密码。成功返回账号记录，失败抛 `LoginFailure`。

    **调用方必须把 `LoginFailure` 一律翻译成同一句用户可见文案** ——
    `reason` 只该进服务端日志（见模块 docstring 的"同一句话"那段）。
    """
    store = _require_store()
    try:
        user = store.find_user(username)
    except AuthStoreError as exc:
        raise LoginFailure(f"账号库不可用: {exc}") from exc

    if user is None:
        # 用户不存在也花掉一次真实哈希的时间，堵住时间侧信道。
        _burn_password_time(password)
        raise LoginFailure("用户名不存在")

    if not user.is_active:
        _burn_password_time(password)
        raise LoginFailure("账号已停用")

    if not verify_password(
        password,
        hash_hex=user.password_hash,
        salt_hex=user.salt,
        iterations=user.iterations,
    ):
        raise LoginFailure("密码不正确")

    return user


def create_session(user_id: int, *, user_agent: str | None = None) -> tuple[str, datetime]:
    """给账号签发一条会话，返回 `(token 明文, 过期时刻)`。

    **token 明文只在这一次返回**，库里存的是它的 SHA-256。
    也就是说服务端自己也恢复不出这个 token —— 这正是我们想要的。
    """
    settings = get_settings()
    store = _require_store()
    token = new_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.auth_session_seconds)
    store.ensure_schema()
    store.create_session(
        token_hash=token_fingerprint(token),
        user_id=user_id,
        expires_at=expires_at,
        user_agent=user_agent,
    )
    return token, expires_at


def resolve_session(token: str | None) -> SessionUser | None:
    """把 cookie 里的 token 换成身份。任何一步不通过都返回 `None`。

    五道检查，缺一不可：
      ① token 非空；② 库里有这条会话；③ 会话没过期；④ 账号还在；
      ⑤ 账号没被停用。
    第 ④⑤ 条经常被漏掉 —— 结果是"账号删了但旧 cookie 还能用 8 小时"。

    **例外**：如果带了 token、但账号库根本没配置，这里**不返回 None，而是抛 503**。
    返回 None 会被上层翻译成"你没登录"（401），可实际故障是"这个部署没接账号库" ——
    用户照着 401 去登录，只会撞上 `/api/auth/login` 的 503，白折腾一圈。
    没有 token（就是没登录）时不受影响，仍然是普通的 401。
    """
    if not token:
        return None
    store = _require_store()
    try:
        session = store.get_session(token_fingerprint(token))
    except AuthStoreError:
        # 库连不上时**拒绝**，不是放行。把故障变成越权是最糟的失败方式。
        return None
    if session is None:
        return None

    try:
        expires_at = datetime.fromisoformat(session.expires_at)
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None

    try:
        user = store.get_user(session.user_id)
    except AuthStoreError:
        return None
    if user is None or not user.is_active:
        return None

    return SessionUser(id=user.id, username=user.username, display_name=user.display_name)


def destroy_session(token: str | None) -> None:
    """退出登录。删不到也算成功（幂等）。

    库故障时**不上抛**：用户点了"退出"，即使库挂了也应该把 cookie 清掉 ——
    否则他会滞留在一个"看起来还登录着"的界面里。
    """
    if not token:
        return
    store = get_auth_store()
    if store is None:
        return
    try:
        store.delete_session(token_fingerprint(token))
    except AuthStoreError:
        return


def session_cookie_kwargs(*, max_age: int) -> dict[str, object]:
    """`response.set_cookie()` 的参数。集中一处，避免登录与登出写得不一样。

    - `httponly=True`：**JS 读不到这个 cookie**（`document.cookie` 里没有它）。
      这是防 XSS 偷 token 的第一道墙 —— 前端代码也不该碰它。
    - `samesite="lax"`：跨站发起的 POST **不会**带上它，这是本项目防 CSRF 的主要机制
      （详见 docs/DECISIONS.md 里关于 CSRF 的那条：单靠 SameSite 不是完整的
      CSRF 防护，但配合"所有写接口都校验会话 + 没有跨站表单"是当前架构下
      相称的选择，残余风险已记录）。
    - `secure`：见 `Settings.auth_cookie_secure` 的说明 —— 本机 http 必须是 False。
    - `path="/"`：整个站点都带，否则 `/api/...` 的请求带不上它。
    """
    settings = get_settings()
    return {
        "key": settings.auth_cookie_name,
        "max_age": max_age,
        "httponly": True,
        "samesite": "lax",
        "secure": settings.auth_cookie_secure,
        "path": "/",
    }


# ---------------------------------------------------------------------------
# FastAPI 依赖：受保护路由
# ---------------------------------------------------------------------------
def current_user_or_none(request: Request) -> SessionUser | None:
    """从请求 cookie 解析当前用户。没登录返回 `None`（不抛异常）。"""
    settings = get_settings()
    token = request.cookies.get(settings.auth_cookie_name)
    return resolve_session(token)


def require_user(request: Request) -> SessionUser:
    """受保护路由的依赖：没登录直接 401。

    用它而不是"在路由函数里手写 if"：忘写一次就是一个裸奔的接口，
    而**没有任何测试会发现漏掉的那个**。挂成依赖之后，路由要么要登录、
    要么就 401，不存在"忘了"这个中间态。

    401 而不是 403：403 的意思是"我知道你是谁，但你不能看这个"，
    而这里的情况是"我还不知道你是谁"。前端据此决定是弹登录页还是报权限错误。
    """
    user = current_user_or_none(request)
    if user is None:
        raise AppError(ErrorCode.UNAUTHORIZED, "请先登录")
    return user
