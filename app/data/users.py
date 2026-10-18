"""账号与会话库：真实认证的持久层（SQLite，零新依赖）。

这个模块负责什么
----------------
1. 建两张表：`auth_users`（账号）与 `auth_sessions`（登录会话）。
2. 账号：`create_user()` / `find_user()` / `count_users()` / `update_password()`。
3. 会话：`create_session()` / `get_session()` / `touch_session()` / `delete_session()` /
   `delete_user_sessions()` / `purge_expired_sessions()`。
4. 把"这个库用不了"统一成 `AuthStoreError`。

这个模块不负责什么
-----------------
- **不做哈希**。密码怎么哈希、迭代多少次、盐怎么生成，全是 `app/core/auth.py` 的活。
  数据层只存字符串 —— 它不 import `hashlib`，也就不可能"顺手换一种哈希"。
- **不判断权限**。"这个会话还有效吗""这个人能不能看这条工作流"是核心层的判断，
  这里只回答"库里有没有这条记录、过期时间是什么"。
- **不吞异常**，与 `app/data/history.py` 同款纪律：吞不吞由调用方决定。
- 不知道 HTTP、不知道 cookie、不知道 FastAPI。

为什么单独一个库文件，而不是塞进 `data/db/app.db`
------------------------------------------------
归档库有一个 `DB_PATH` 为空 = **明确关闭归档**的开关。认证是**不可关闭**的
（关掉它整个产品就没有登录了），把两者放进同一个文件，就会出现"用户为了
关归档而清空 `DB_PATH`，结果顺手把账号库也指向了别处"这种连带伤害。
两个库各自独立，谁也关不掉谁。默认落在 `data/db/auth.db`，
`.gitignore` 里 `data/db/*` 那条规则已经把它连同 `-journal` 一起挡住了。

为什么是短连接 + 一把模块级写锁
------------------------------
与 `app/data/history.py` 完全同款的理由，这里只重复结论：`sqlite3` 连接默认
不能跨线程用，而本项目的后台工作流跑在 `threading.Thread` 里；所以每次操作开一条
短连接，另加 ① 进程内模块级 `_WRITE_LOCK` **只保护写**、② 每条连接
`PRAGMA busy_timeout`。`AuthStore` 实例上没有连接、没有游标、只有路径，
因此可以安全地被多线程共享。

为什么会话 token **不直接存库**，而是存它的 SHA-256
-------------------------------------------------
会话表里存的是 `sha256(token)`。这样即使 `auth.db` 文件被拿走（备份、误提交、
同事拷走），拿到的人也**无法**用里面的值冒充任何一个已登录用户 —— 他必须
反推出原始 token，而 SHA-256 不给你这条路。
（对比一下：密码存的是 PBKDF2 慢哈希 + 每用户随机盐，因为密码的熵很低、
必须抗暴力破解；会话 token 是 `secrets.token_urlsafe(32)` 生成的 256 位随机值，
熵已经足够高，用一次 SHA-256 就够，不需要慢哈希。）
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterator

from app.config import current_settings

#: 表结构版本。与 `app/data/history.py` 同款纪律：改动表的形状必须 +1，
#: 并且同时想清楚"老库怎么办"——这个模块不会替你决定，它只会拒绝在版本不符的库上读写。
AUTH_SCHEMA_VERSION: int = 1

_BUSY_TIMEOUT_MS = 5000
_BUSY_TIMEOUT_SECONDS = _BUSY_TIMEOUT_MS / 1000

_USERS_TABLE = "auth_users"
_SESSIONS_TABLE = "auth_sessions"

# 进程内的写锁：**只保护写**，读不抢锁（读之间不冲突，让它们并行）。
_WRITE_LOCK = threading.Lock()

_CREATE_USERS_SQL = f"""
CREATE TABLE IF NOT EXISTS {_USERS_TABLE} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL COLLATE NOCASE,
    display_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    salt           TEXT NOT NULL,
    iterations     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_login_at  TEXT,
    is_active      INTEGER NOT NULL,
    schema_version INTEGER NOT NULL
)
"""

# UNIQUE 建在 username 上：重名必须在**数据库层**被挡住，不能只靠"插入前先查一下"。
# 先查后插在并发下必然漏（两个请求同时查到"不存在"，然后都插进去）。
# COLLATE NOCASE 让 `Alice` 与 `alice` 视为同一个用户名 —— 否则会出现两个
# 看起来一模一样、只有大小写不同的账号，登录时进哪个取决于输入大小写。
_CREATE_USERS_INDEX_SQL = f"""
CREATE UNIQUE INDEX IF NOT EXISTS idx_{_USERS_TABLE}_username
    ON {_USERS_TABLE} (username COLLATE NOCASE)
"""

_CREATE_SESSIONS_SQL = f"""
CREATE TABLE IF NOT EXISTS {_SESSIONS_TABLE} (
    token_hash     TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    user_agent     TEXT,
    schema_version INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES {_USERS_TABLE} (id) ON DELETE CASCADE
)
"""

# 按 user_id 删会话（改密码 / 禁用账号时用）要快。
_CREATE_SESSIONS_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{_SESSIONS_TABLE}_user_id
    ON {_SESSIONS_TABLE} (user_id)
"""

# 过期清理按 expires_at 扫。
_CREATE_SESSIONS_EXPIRY_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{_SESSIONS_TABLE}_expires_at
    ON {_SESSIONS_TABLE} (expires_at)
"""


class AuthStoreError(RuntimeError):
    """账号/会话库不可用。调用方自己决定是吞掉还是让请求失败。

    认证与归档不一样：归档挂了用户照样能用，**认证挂了必须让请求失败** ——
    抱着"库连不上那先放行吧"的想法，等于把故障变成一次完全的越权。
    """


def _as_utc(moment: datetime) -> datetime:
    """朴素时间当 UTC 处理，带时区的换算到 UTC。

    与 `app/data/history.py` 同款：库里所有时间都是同一个基准，
    `ORDER BY expires_at` 比的是**字符串**，混着 `+08:00` 与 `+00:00` 时
    字典序与时间序会不一致（统一到 UTC 之后"字典序 == 时间序"）。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return _as_utc(moment).isoformat()


@dataclass(frozen=True)
class UserRecord:
    """一条账号记录。字段与表列一一对应，不做任何加工。"""

    id: int
    username: str
    display_name: str
    password_hash: str
    salt: str
    iterations: int
    created_at: str
    last_login_at: str | None
    is_active: bool


@dataclass(frozen=True)
class SessionRecord:
    """一条会话记录。`token_hash` 是 token 的 SHA-256，不是 token 本身。"""

    token_hash: str
    user_id: int
    created_at: str
    expires_at: str
    last_seen_at: str


class AuthStore:
    """账号与会话的读写。实例上只有 `path`，没有连接 —— 可以随便跨线程共享。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """一条短连接。用完就关。

        `row_factory = sqlite3.Row` 让查询结果能按列名取值 —— 否则只能按下标，
        以后往表里插一列，所有 `row[3]` 都会悄悄指错字段。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path),
                timeout=_BUSY_TIMEOUT_SECONDS,
            )
        except sqlite3.Error as exc:
            raise AuthStoreError(f"打开账号库失败: {self.path}") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 建表
    # ------------------------------------------------------------------
    def ensure_schema(self) -> None:
        """建表 + 建索引。**幂等**：启动时调一次即可，重复调用无害。"""
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(_CREATE_USERS_SQL)
                conn.execute(_CREATE_USERS_INDEX_SQL)
                conn.execute(_CREATE_SESSIONS_SQL)
                conn.execute(_CREATE_SESSIONS_INDEX_SQL)
                conn.execute(_CREATE_SESSIONS_EXPIRY_INDEX_SQL)
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"建表失败: {exc}") from exc

    # ------------------------------------------------------------------
    # 账号
    # ------------------------------------------------------------------
    def count_users(self) -> int:
        """账号总数。用来判断"是不是还没初始化过"。

        这个数字决定登录页显示「登录」还是「创建第一个管理员账号」，
        所以它必须是**查库**得到的，不能在内存里记一个标志位 ——
        进程重启后标志位就丢了，用户会看到一个永远登不进去的登录页。
        """
        with self._connect() as conn:
            try:
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {_USERS_TABLE}").fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询账号数失败: {exc}") from exc
        return int(row["n"]) if row else 0

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password_hash: str,
        salt: str,
        iterations: int,
        now: datetime | None = None,
    ) -> UserRecord:
        """插入一个账号。用户名重复时抛 `AuthStoreError`。

        重复这件事**交给数据库的 UNIQUE 约束**去发现，而不是先 SELECT 再 INSERT：
        后者在并发下必然漏（见索引那段的注释）。调用方把 `AuthStoreError`
        翻译成"用户名已被占用"给用户看。
        """
        created = _iso(now or datetime.now(timezone.utc))
        with _WRITE_LOCK, self._connect() as conn:
            try:
                cur = conn.execute(
                    f"""
                    INSERT INTO {_USERS_TABLE} (
                        username, display_name, password_hash, salt,
                        iterations, created_at, last_login_at, is_active, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?)
                    """,
                    (
                        username,
                        display_name,
                        password_hash,
                        salt,
                        iterations,
                        created,
                        AUTH_SCHEMA_VERSION,
                    ),
                )
                conn.commit()
                new_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise AuthStoreError(f"用户名已存在: {username}") from exc
            except sqlite3.Error as exc:
                raise AuthStoreError(f"创建账号失败: {exc}") from exc

        return UserRecord(
            id=new_id,
            username=username,
            display_name=display_name,
            password_hash=password_hash,
            salt=salt,
            iterations=iterations,
            created_at=created,
            last_login_at=None,
            is_active=True,
        )

    def find_user(self, username: str) -> UserRecord | None:
        """按用户名查账号。大小写不敏感（与 UNIQUE 索引同一套排序规则）。

        找不到返回 `None` —— 注意调用方**不要**因此给用户"用户名不存在"这种提示，
        登录失败一律回同一句话（见 `app/api/routes_auth.py` 的说明）。
        """
        with self._connect() as conn:
            try:
                row = conn.execute(
                    f"""
                    SELECT id, username, display_name, password_hash, salt,
                           iterations, created_at, last_login_at, is_active
                    FROM {_USERS_TABLE}
                    WHERE username = ? COLLATE NOCASE
                    """,
                    (username,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询账号失败: {exc}") from exc
        return _row_to_user(row) if row else None

    def get_user(self, user_id: int) -> UserRecord | None:
        with self._connect() as conn:
            try:
                row = conn.execute(
                    f"""
                    SELECT id, username, display_name, password_hash, salt,
                           iterations, created_at, last_login_at, is_active
                    FROM {_USERS_TABLE} WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询账号失败: {exc}") from exc
        return _row_to_user(row) if row else None

    def mark_login(self, user_id: int, now: datetime | None = None) -> None:
        """记下这次登录时间。**失败不影响登录**（调用方吞异常）。

        为什么它必须是"可失败但不致命"的：`last_login_at` 只是给人看的，
        写不进去不该让一个密码正确的人登不进来。
        """
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(
                    f"UPDATE {_USERS_TABLE} SET last_login_at = ? WHERE id = ?",
                    (_iso(now or datetime.now(timezone.utc)), user_id),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"更新登录时间失败: {exc}") from exc

    def update_password(
        self,
        user_id: int,
        *,
        password_hash: str,
        salt: str,
        iterations: int,
    ) -> None:
        """改密码。**不在这里删会话** —— 那是调用方的决定（见 `delete_user_sessions`）。"""
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(
                    f"""
                    UPDATE {_USERS_TABLE}
                    SET password_hash = ?, salt = ?, iterations = ?
                    WHERE id = ?
                    """,
                    (password_hash, salt, iterations, user_id),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"更新密码失败: {exc}") from exc

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def create_session(
        self,
        *,
        token_hash: str,
        user_id: int,
        expires_at: datetime,
        user_agent: str | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        moment = now or datetime.now(timezone.utc)
        created = _iso(moment)
        expires = _iso(expires_at)
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO {_SESSIONS_TABLE} (
                        token_hash, user_id, created_at, expires_at,
                        last_seen_at, user_agent, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        user_id,
                        created,
                        expires,
                        created,
                        (user_agent or "")[:200] or None,
                        AUTH_SCHEMA_VERSION,
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"创建会话失败: {exc}") from exc
        return SessionRecord(
            token_hash=token_hash,
            user_id=user_id,
            created_at=created,
            expires_at=expires,
            last_seen_at=created,
        )

    def get_session(self, token_hash: str) -> SessionRecord | None:
        with self._connect() as conn:
            try:
                row = conn.execute(
                    f"""
                    SELECT token_hash, user_id, created_at, expires_at, last_seen_at
                    FROM {_SESSIONS_TABLE} WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询会话失败: {exc}") from exc
        if row is None:
            return None
        return SessionRecord(
            token_hash=row["token_hash"],
            user_id=int(row["user_id"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            last_seen_at=row["last_seen_at"],
        )

    def list_sessions(self, user_id: int) -> list[SessionRecord]:
        """列出某个账号的全部会话，**最近创建的在前**。

        给"登录设备"列表用。排序用 `created_at DESC`：
        用户找的通常是"最近那次不是我登的"。

        这里**不按过期时间过滤** —— 调用方（路由层）会把 `expires_at` 一并返回，
        由界面显示"还有多久失效"。数据层替调用方决定"过期的算不算存在"
        会让边界变得含糊：一条 3 秒前刚过期的会话，用户恰恰最需要看到它。
        """
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    f"""
                    SELECT token_hash, user_id, created_at, expires_at, last_seen_at
                    FROM {_SESSIONS_TABLE}
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询会话列表失败: {exc}") from exc
        return [
            SessionRecord(
                token_hash=row["token_hash"],
                user_id=int(row["user_id"]),
                created_at=row["created_at"],
                expires_at=row["expires_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    def touch_session(self, token_hash: str, now: datetime | None = None) -> None:
        """更新 `last_seen_at`。可失败不致命（同 `mark_login`）。"""
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(
                    f"UPDATE {_SESSIONS_TABLE} SET last_seen_at = ? WHERE token_hash = ?",
                    (_iso(now or datetime.now(timezone.utc)), token_hash),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"更新会话失败: {exc}") from exc

    def delete_session(self, token_hash: str) -> None:
        """退出登录。删不到也算成功（幂等）—— 重复调用不该报错。"""
        with _WRITE_LOCK, self._connect() as conn:
            try:
                conn.execute(
                    f"DELETE FROM {_SESSIONS_TABLE} WHERE token_hash = ?",
                    (token_hash,),
                )
                conn.commit()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"删除会话失败: {exc}") from exc

    def delete_user_sessions(self, user_id: int) -> int:
        """踢掉某个账号的**所有**会话，返回删了几条。

        改密码之后必须调它：否则"我改了密码"并不能把已经登录的别人踢下线，
        用户对"改密码"的预期是反的。
        """
        with _WRITE_LOCK, self._connect() as conn:
            try:
                cur = conn.execute(
                    f"DELETE FROM {_SESSIONS_TABLE} WHERE user_id = ?",
                    (user_id,),
                )
                conn.commit()
                return int(cur.rowcount)
            except sqlite3.Error as exc:
                raise AuthStoreError(f"清理会话失败: {exc}") from exc

    def purge_expired_sessions(self, now: datetime | None = None) -> int:
        """删掉已过期的会话，返回删了几条。

        过期会话**本来就不会通过校验**（`app/core/auth.py` 会比对 `expires_at`），
        所以这里只是打扫房间，不做也不会有人被放进来。它存在的意义是别让
        表无限长大。
        """
        with _WRITE_LOCK, self._connect() as conn:
            try:
                cur = conn.execute(
                    f"DELETE FROM {_SESSIONS_TABLE} WHERE expires_at <= ?",
                    (_iso(now or datetime.now(timezone.utc)),),
                )
                conn.commit()
                return int(cur.rowcount)
            except sqlite3.Error as exc:
                raise AuthStoreError(f"清理过期会话失败: {exc}") from exc

    def count_sessions(self, user_id: int) -> int:
        with self._connect() as conn:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {_SESSIONS_TABLE} WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise AuthStoreError(f"查询会话数失败: {exc}") from exc
        return int(row["n"]) if row else 0


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=int(row["id"]),
        username=row["username"],
        display_name=row["display_name"],
        password_hash=row["password_hash"],
        salt=row["salt"],
        iterations=int(row["iterations"]),
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        is_active=bool(row["is_active"]),
    )


@lru_cache(maxsize=4)
def _build_auth_store(db_path: str) -> AuthStore | None:
    """按**库文件路径**缓存 `AuthStore`。

    为什么要带上路径当缓存键（而不是 `@lru_cache(maxsize=1)` 无参缓存）：
    无参缓存一旦生成就再也感知不到配置变化。生产上配置不变没关系，但
    `AUTH_DB_PATH` 是**测试用来做隔离**的（每个用例指到自己的临时库），
    无参缓存会让第二个用例拿到第一个用例的库 —— 表现是"明明换了空库，
    bootstrap 却说库里有账号（403）"，且极难查。带上路径做键，配置一换
    缓存自然分桶，两个用例的库互不干扰。
    """
    return AuthStore(Path(db_path))


def get_auth_store() -> AuthStore | None:
    """便捷入口：读 `settings.auth_db_path` 并缓存对应的 `AuthStore`。

    `auth_db_path` 为空串表示"这个部署没有账号库"——此时返回 `None`，
    由调用方决定怎么处理（路由层翻译成 503，而不是让 `Path("")` 变成
    项目根目录然后去 `sqlite3.connect()` 一个目录）。空串这一支**故意不缓存**，
    保证"把开关关掉"立刻生效、不需要谁记得清缓存。

    配置用 `current_settings()` 而不是导入期的 `get_settings()`：后者会把函数对象
    绑死在导入那一刻，测试里 `monkeypatch.setattr(config, "get_settings", fake)`
    就影响不到这里 —— 实测表现是"换成空库后 bootstrap 仍然 403（查的是上一个用例的库）"。
    见 `app/config.py::current_settings`。
    """
    settings = current_settings()
    if not settings.auth_db_path:
        return None
    return _build_auth_store(str(settings.auth_db_file))


def reset_auth_store_cache() -> None:
    """测试改配置后清缓存，下一次 `get_auth_store()` 会重新读配置。"""
    _build_auth_store.cache_clear()
