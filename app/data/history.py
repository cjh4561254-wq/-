"""工作流历史归档库：把每条跑完的工作流落进一个 SQLite 文件。

这个模块负责什么
----------------
1. 用标准库 `sqlite3` 建一张 `workflow_history` 表：**一条工作流一行**。
2. `archive()` 按 `workflow_id` **UPSERT** —— 同一条工作流被归档两次
   （orchestrator 跑完归档一次、超时轮询又归档一次）不留第二行、也不报错。
3. `get()` / `list_recent()` / `count()` 三种读法；列表按
   `finished_at DESC NULLS LAST, created_at DESC` 排序、支持 `limit` / `offset`。
4. 把"这个库用不了"统一成 `HistoryStoreError`，让调用方一眼看出该不该吞。

这个模块不负责什么
------------------
- **不生成** `HistoryRecord`：把 `WorkflowState` 翻译成记录是调用方的活
  （`app/core/archive.py`）。数据层只认记录，不认工作流状态机、不认 pydantic。
- **不吞异常**。"归档失败要不要拖垮工作流"是调用方的判断，这里一律抛
  （调用方的策略是"只记 warning，工作流照常结束"）。
- **不做迁移**：表里存的 `schema_version` 与代码里的 `HISTORY_SCHEMA_VERSION`
  不符就抛错，不静默改表、不 `DROP TABLE`。
- 不知道 HTTP、不知道 FastAPI、不知道配置从哪来（唯一例外是
  `get_history_store()` 这个便捷入口，它读一次 `settings.db_path`）。

为什么是标准库 `sqlite3` 而不是 SQLAlchemy
-----------------------------------------
`docs/ARCHITECTURE.md` 第 14 节决策 3 拍板了"做 SQLite 历史归档、SSE 不做"，
但那句里写的"用**已装的** SQLAlchemy"是错的：`requirements.txt` 里没有它，
`import sqlalchemy` 直接 `ModuleNotFoundError`。归档总共一张表、六个操作，
标准库 `sqlite3` 足够（本机实测 3.45.3），**零新依赖** —— 为一个单表归档引入
一个 ORM + 引擎 + 会话层，是拿部署复杂度换几十行代码。

为什么是短连接 + 一把模块级写锁（本模块的核心设计）
--------------------------------------------------
`sqlite3` 的连接默认**不能跨线程用**（`check_same_thread=True` 时跨线程调用直接
抛 `ProgrammingError`）。本项目的工作流跑在 `threading.Thread` 里（见
`app/core/background.py`），100 条工作流就是 100 个线程。于是：

- **不用**"长连接 + `check_same_thread=False`"共享一个连接：那等于关掉 sqlite3
  自己的线程检查，然后指望 SQLite 的文件锁挡住并发写 —— 出问题时是难查的数据
  竞争，而不是一个异常。改成**每次操作开一条短连接**（`connect()` → 用 → `close()`）：
  打开一个本地文件很廉价，而"每个线程用自己的连接"天然满足 sqlite3 的要求。
- 短连接之间仍会抢同一个库文件的写锁，所以再加两道：
  ① 进程内的模块级 `_WRITE_LOCK`，**只保护写**（`archive()` 与建表），读不抢锁；
  ② 每条连接都设 `PRAGMA busy_timeout = 5000`，让写冲突**等一会儿**而不是立刻
     `database is locked`。①管进程内、②管跨进程（单进程演示用不上，多进程部署时
     是最后一道）。

`HistoryStore` 实例上**没有**连接、没有游标、没有可变状态，只有一个路径 ——
所以它本身可以随便跨线程共享（`get_history_store()` 缓存一个实例是安全的）。

为什么不开 WAL
-------------
WAL 会额外生成 `-wal` / `-shm` 两个文件。默认的 `journal_mode=delete` 只在写事务
期间生成 `-journal`，事务一结束就删。对一个"要被面试官拉下来跑一遍"的项目，
多两个文件没有任何收益，所以这里**不设** `journal_mode`（保持 SQLite 默认）。

为什么时间存 ISO-8601 字符串、而且统一折算成 UTC
-----------------------------------------------
不用 Unix 时间戳（读库时肉眼不可读、时区信息丢失），也不存本地时间字符串
（"这台机器上的 09:00"在三台机器上是三个时刻）。
`datetime.isoformat()` / `datetime.fromisoformat()` 是标准库自带的**可逆**一对。

写进去之前统一 `.astimezone(timezone.utc)`，有两个理由：
1. "不许存本地时间" —— 库里所有时间都是同一个基准，读的人不用猜。
2. `ORDER BY finished_at` 比的是**字符串**。混着 `+08:00` 与 `+00:00` 时，
   字典序与时间序会不一致（`2026-10-14T09:00:00+08:00` 早于
   `2026-10-14T02:00:00+00:00`，字符串比较却给出相反结果）。统一到 UTC 之后
   "字典序 == 时间序"，排序可以放心交给 SQLite（也就才能用 `LIMIT/OFFSET`）。

朴素时间（`tzinfo is None`）当 UTC 处理，与 `app/api/routes_workflows.py:283`
的 `_as_utc` 同款 —— 测试里手写 `datetime(2026, 10, 13)` 是自然写法，
不该因为"少写了时区"就在数据层炸掉。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

#: 表结构版本。每一行都写一份；读的时候与它比对，不符就抛 `HistoryStoreError`。
#: 改动表的形状（加列、改类型、改语义）时**必须**加一，并且同时想清楚"老库怎么办"
#: —— 这个模块不会替你决定，它只会拒绝在版本不符的库上继续读写。
HISTORY_SCHEMA_VERSION: int = 1

#: `PRAGMA busy_timeout` 的毫秒数：写冲突时最多等这么久再报 `database is locked`。
_BUSY_TIMEOUT_MS = 5000

#: `sqlite3.connect(timeout=)` 的秒数。与上面的毫秒数是**同一件事的两处写法**：
#: `connect` 的超时管"拿连接"，`busy_timeout` 管"语句撞上锁"，两个都设，
#: 免得只设了一处、另一处悄悄用默认值（默认也是 5 秒，但那是巧合，不是约定）。
_BUSY_TIMEOUT_SECONDS = _BUSY_TIMEOUT_MS / 1000

_TABLE_NAME = "workflow_history"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    workflow_id    TEXT PRIMARY KEY,
    question       TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    finished_at    TEXT,
    has_report     INTEGER NOT NULL,
    llm_used       INTEGER NOT NULL,
    report_text    TEXT,
    error_code     TEXT,
    schema_version INTEGER NOT NULL,
    archived_at    TEXT NOT NULL,
    owner_user_id  INTEGER,
    owner_username TEXT
)
"""

#: 归属两列是**后加的**（数据隔离改造）。老库（本次改造之前建的）里没有它们，
#: `ensure_schema()` 会用 `ALTER TABLE ADD COLUMN` 补上 —— 见 `_ADD_OWNER_COLUMNS_SQL`
#: 与 `_ensure_owner_columns()`。这就是为什么 `HISTORY_SCHEMA_VERSION` **不加一**：
#: 加两列是纯增量、不改变已有列的含义，老代码读新库也只是少看到两列，
#: 而"版本号 +1"会让所有已存在的库直接拒绝服务（版本检查是精确相等比对），
#: 代价是用户的历史功能突然 500，收益只是"版本号看起来更整齐"。
#:
#: 三列的 NOT NULL 情况要分清（写错了会很痛）：
#: - `owner_user_id` / `owner_username` **故意可空**：老行补列时只能填 NULL，
#:   而"给老数据编一个归属"是伪造数据 —— NULL 的语义是"没有归属 → 谁也不给看"。
#: - 新写入的行由代码保证一定带 owner（`archive()` 的参数里就有）。
_ADD_OWNER_COLUMNS_SQL: tuple[str, ...] = (
    f"ALTER TABLE {_TABLE_NAME} ADD COLUMN owner_user_id INTEGER",
    f"ALTER TABLE {_TABLE_NAME} ADD COLUMN owner_username TEXT",
)

_OWNER_COLUMNS: tuple[str, ...] = ("owner_user_id", "owner_username")

# 列表按 finished_at 倒序，所以索引建在它上面（升序索引也能反着扫）。
_CREATE_INDEX_SQL = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE_NAME}_finished_at
    ON {_TABLE_NAME} (finished_at DESC)
"""

# 重复归档必须无害：同一条工作流可能被 orchestrator 归档一次、又被超时轮询
# 归档一次。`ON CONFLICT(workflow_id) DO UPDATE` 让"最后一次写入"胜出，
# 而不是抛 IntegrityError 或者留两行。
_UPSERT_SQL = f"""
INSERT INTO {_TABLE_NAME} (
    workflow_id, question, status, created_at, finished_at,
    has_report, llm_used, report_text, error_code, schema_version, archived_at,
    owner_user_id, owner_username
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(workflow_id) DO UPDATE SET
    question       = excluded.question,
    status         = excluded.status,
    created_at     = excluded.created_at,
    finished_at    = excluded.finished_at,
    has_report     = excluded.has_report,
    llm_used       = excluded.llm_used,
    report_text    = excluded.report_text,
    error_code     = excluded.error_code,
    schema_version = excluded.schema_version,
    archived_at    = excluded.archived_at,
    owner_user_id  = excluded.owner_user_id,
    owner_username = excluded.owner_username
"""

_SELECT_COLUMNS = (
    "workflow_id, question, status, created_at, finished_at, "
    "has_report, llm_used, report_text, error_code, archived_at, "
    "owner_user_id, owner_username"
)

# 排序：`finished_at IS NULL` 升序（0 在前、1 在后）→ 没跑完的排最后；
# 再按时间倒序、最后用 created_at 兜底（同一秒完成的两条也要有确定顺序）。
#
# 这里**不写** `NULLS LAST` 字面量：那是 SQLite 3.30+ 的语法，而
# `x IS NULL` 在任何版本上都等价 —— 少一个"运行环境的 SQLite 够不够新"的前提。
_ORDER_BY_SQL = "ORDER BY finished_at IS NULL, finished_at DESC, created_at DESC"

#: 只保护写操作（`archive()` + 建表）。读**不抢锁**，所以列表/单条查询不会被归档阻塞。
#: 用不可重入的 `threading.Lock`（不是 RLock）是刻意的：这一层不允许"锁里再拿一次
#: 同一把锁"的写法，`_ensure_schema_locked()` 这个名字就是在提醒调用方已经持锁。
_WRITE_LOCK = threading.Lock()


class HistoryStoreError(RuntimeError):
    """归档库不可用（路径是目录、磁盘只读、schema 版本不符、SQL 失败……）。"""


@dataclass(frozen=True)
class HistoryRecord:
    """一条工作流的归档记录。

    数据层只认这个结构：它不知道 `WorkflowState`、不知道 `FinalReport`，
    也不知道 `status` 的取值是怎么来的（调用方传 `WorkflowStatus.value`，
    例如 `"success"` / `"failed"`）。

    ## `owner_user_id` 为什么**必填**，而 `owner_username` 可空

    归属是数据隔离的**唯一依据**。把它写成带默认值的字段，等于允许
    "忘记传 owner"的代码写出来并且不报错 —— 而那种记录会变成看不见的孤儿
    （NULL owner 谁也不给看），排查起来只能靠翻库。所以它是必填的：
    所有构造点都必须显式回答"这条记录是谁的"，漏了当场 `TypeError`。

    类型是 `int | None`，不是 `int`：**老库里的行没有归属**（改造之前没有"用户"
    这个概念），它们读出来就是 `None`。写入口径上仍然是"必须显式传"
    （没有默认值）；只有"从库里读出来"这一条路径能产生 `None`，
    而它会被查询层的 `WHERE owner_user_id = ?` 自动过滤掉，永远到不了接口。

    `owner_username` 只是**展示用的冗余字段**（前端要显示"谁跑的"，
    而不必为了一个名字再去查一次账号库）。它不参与任何判定 ——
    用户名可以改，用户 id 不会。所以它可以为空（库里存的老记录没有它）。
    """

    workflow_id: str
    question: str
    status: str
    created_at: datetime
    finished_at: datetime | None
    has_report: bool
    llm_used: bool
    report_text: str | None
    error_code: str | None
    archived_at: datetime
    owner_user_id: int | None
    owner_username: str | None = None


class HistoryStore:
    """一个 SQLite 文件上的归档库。**每次操作一条短连接**（理由见模块 docstring）。"""

    def __init__(self, db_path: str | Path) -> None:
        # 这里**不建目录、不连库**，只记路径：真正落盘发生在第一次用到时
        # （`ensure_schema()`）。于是"路径写错了"（比如 DB_PATH 指向一个目录）
        # 在第一次归档时炸，而不是在 import / 启动时把整个应用带崩。
        self._db_path = Path(db_path)

    @property
    def path(self) -> Path:
        """归档库文件的**绝对**路径（相对路径按当前工作目录解析）。"""
        p = self._db_path
        return p if p.is_absolute() else p.resolve()

    # ---- 建表 ---------------------------------------------------------------

    def ensure_schema(self) -> None:
        """建表 + 建索引，**幂等**；父目录不存在时创建。

        表里已有的 `schema_version` 与 `HISTORY_SCHEMA_VERSION` 不符时抛
        `HistoryStoreError`：不静默迁移、不删表重建 —— 库里可能有用户的历史数据，
        "代码升级了就把旧表删掉"是数据层最不该出现的行为。
        """
        with _WRITE_LOCK:
            self._ensure_schema_locked()

    def _ensure_schema_locked(self) -> None:
        """`ensure_schema()` 的实现，**调用方必须已经持有 `_WRITE_LOCK`**。

        单独拆一个不带锁的版本，是因为 `archive()` 已经在锁里了：
        再调用一次 `ensure_schema()` 会自己等自己（`threading.Lock` 不可重入）→ 死锁。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 只读盘、路径被别的文件占了、没有权限……
            raise HistoryStoreError(f"归档库目录不可用（{self.path.parent}）：{exc}") from exc

        with self._connection() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            self._ensure_owner_columns(conn)
            # "库里已有数据的版本号以表里存的为准"：查的是**实际的行**，
            # 而不是某个元数据表（空库 = 没有行 = 没有版本，直接放行）。
            versions = {
                int(row["schema_version"])
                for row in conn.execute(f"SELECT DISTINCT schema_version FROM {_TABLE_NAME}")
            }

        unexpected = versions - {HISTORY_SCHEMA_VERSION}
        if unexpected:
            raise HistoryStoreError(
                f"归档库的 schema 版本不符（{self.path}）：库里是 {sorted(versions)}，"
                f"代码期望 {HISTORY_SCHEMA_VERSION}。不自动迁移、不删表，"
                "请人工确认后再处理。"
            )

    def _ensure_owner_columns(self, conn: sqlite3.Connection) -> None:
        """给老库补上归属两列（幂等）。**调用方必须已经持有 `_WRITE_LOCK`。**

        为什么这一步是"允许的迁移"，而这个模块的 docstring 又写着"不做迁移"：
        两句话不矛盾，界线是**会不会丢/改用户的数据**。

        - `ALTER TABLE ADD COLUMN` 只加列，已有行在**新列上是 NULL**，
          其余列一个字节都不动；老代码（不认识这两列）读新库照样能读。
        - "把 `schema_version` 不符的库删掉重建" 才是不许做的事 ——
          那会丢历史记录。

        用 NULL 想表达什么，说得再明确一点：**这些行没有归属**（改造之前
        没有"用户"这个概念）。它们不会因此变成"公共数据"—— 查询一律带
        `owner_user_id = ?`，NULL **永远不等于任何 int**（SQL 里 NULL 的比较结果是
        NULL，不是 TRUE），所以老记录对所有用户都不可见。
        "不删用户的数据"和"不给任何人看"同时成立。
        """
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({_TABLE_NAME})").fetchall()
        }
        for column, statement in zip(
            _OWNER_COLUMNS, _ADD_OWNER_COLUMNS_SQL, strict=True
        ):
            if column not in existing:
                conn.execute(statement)

    # ---- 写 ----------------------------------------------------------------

    def archive(self, record: HistoryRecord) -> None:
        """把一条记录写进库（`workflow_id` 冲突时覆盖）。

        重复归档同一条工作流是**正常情况**，不是错误：orchestrator 跑完归档一次，
        超时轮询可能又归档一次（状态从 executing 变成 failed），最后一次胜出。

        归属两列**一起写**（不是"先写一半、稍后补"）：`owner_user_id` 是隔离依据，
        它和记录本体必须是同一次写入 —— 分成两步的话，中间任何失败都会留下
        一条谁也看不见的记录。
        """
        params = (
            record.workflow_id,
            record.question,
            record.status,
            _to_db_time(record.created_at),
            _to_db_time(record.finished_at),
            int(record.has_report),
            int(record.llm_used),
            record.report_text,
            record.error_code,
            HISTORY_SCHEMA_VERSION,
            _to_db_time(record.archived_at),
            record.owner_user_id,
            record.owner_username,
        )
        with _WRITE_LOCK:
            self._ensure_schema_locked()
            with self._connection() as conn:
                conn.execute(_UPSERT_SQL, params)

    # ---- 读 ----------------------------------------------------------------

    def get_for_user(self, workflow_id: str, owner_user_id: int) -> HistoryRecord | None:
        """按 id 取一条**属于这个用户的**记录；没有（或不属于他）返回 `None`。

        为什么过滤条件写在 SQL 里，而不是"先查出来再在路由层比一下 owner"：
        后者会把别人的记录读进内存，任何一个忘了比的调用点就是越权；
        而且 `None` 的含义要单一 —— 只有一种"没有"，路由层就只会写一条 404 分支，
        两条分支的文案也就不可能漂移（见 `app/api/ownership.py`）。

        NULL owner 的老记录**永远不会**被匹配上：SQL 里 `NULL = ?` 的结果是 NULL
        （不是 TRUE），这正是我们要的"谁也不给看"。
        """
        self.ensure_schema()
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                "WHERE workflow_id = ? AND owner_user_id = ?",
                (workflow_id, owner_user_id),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_recent(
        self, limit: int = 20, offset: int = 0, *, owner_user_id: int
    ) -> list[HistoryRecord]:
        """这个用户最近完成的一批（没完成过的排最后），带分页。

        `owner_user_id` 是**关键字必填**：这个方法以前是"列出所有人的记录"，
        那种默认行为在加了隔离之后就成了漏洞 —— 所以这里连默认值都不给，
        少传一个参数就当场 `TypeError`，而不是悄悄返回别人的数据。

        负数是编程错误而不是"用户输入"（这个方法只被我们自己的代码调用），
        所以直接抛 `ValueError`；否则 SQLite 会把 `LIMIT -1` 当"不限制"，
        于是"传错了"变成"quietly 返回全部"，比抛错难查得多。
        """
        if limit < 0 or offset < 0:
            raise ValueError("limit / offset 不能是负数")
        self.ensure_schema()
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} "
                f"WHERE owner_user_id = ? {_ORDER_BY_SQL} LIMIT ? OFFSET ?",
                (owner_user_id, limit, offset),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def count(self, *, owner_user_id: int) -> int:
        """这个用户的归档条数（列表接口的 `total`）。

        与 `list_recent` 同理，`owner_user_id` 必填。**尤其不能退回全表 COUNT**：
        那个数字会直接泄露"系统里一共有多少条别人的分析记录"。
        """
        self.ensure_schema()
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {_TABLE_NAME} WHERE owner_user_id = ?",
                (owner_user_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 连接 --------------------------------------------------------------

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """一条短连接：开 → 用 → 提交 → 关；`sqlite3` / 文件系统错误统一成 `HistoryStoreError`。

        三个要点：
        - `sqlite3.connect()` 每次新建，**不缓存**（连接不能跨线程用）。
        - `PRAGMA busy_timeout` 是**连接级**的，所以每条连接都要设一遍。
        - `finally` 里一定关：长跑进程里漏连接会攒成"打开的文件数过多"。
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_SECONDS)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            yield conn
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            # 故意**不吞**：调用方要能明确知道"这次归档失败了"。
            raise HistoryStoreError(f"归档库操作失败（{self.path}）：{exc}") from exc
        finally:
            if conn is not None:
                conn.close()


def _as_utc(value: datetime) -> datetime:
    """朴素时间补 UTC 时区。

    和 `app/api/routes_workflows.py:283` 的 `_as_utc` 是同一个处理：
    测试里写 `datetime(2026, 10, 13)` 很正常，而"朴素时间 vs 带时区时间"的
    比较/相减会抛 `TypeError` —— 那是把"少写一个 tzinfo"变成 500。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _to_db_time(value: datetime | None) -> str | None:
    """datetime → 库里存的 ISO-8601 字符串（**统一 UTC**，理由见模块 docstring）。"""
    if value is None:
        return None
    return _as_utc(value).astimezone(timezone.utc).isoformat()


def _from_db_time(value: str | None) -> datetime | None:
    """库里存的字符串 → 带时区的 datetime（`None` 原样返回）。"""
    if value is None:
        return None
    # 我们自己写进去的字符串一定带时区；万一有人手工往里塞了朴素字符串，
    # 也按 UTC 处理，免得下游做时间运算时抛 TypeError（和 _as_utc 同一个理由）。
    return _as_utc(datetime.fromisoformat(value))


def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
    """一行 → `HistoryRecord`。`INTEGER` 存的两个布尔要转回 `bool`，不能漏。

    归属列可能是 NULL（老行补列时留下的）。**不要**在这里给 NULL 编一个默认
    owner（比如 0 或"第一个用户"）：编出来的归属会让老数据对某个人突然可见，
    而这正是隔离要防的事。NULL 原样读出来，交给查询层自然过滤掉。

    `owner_user_id` 的类型在"有值"时一定是 int（我们写进去的就是 int），
    所以这里 `int(row["owner_user_id"])` 只在非空分支执行 ——
    写成 `int(row["owner_user_id"] or 0)` 会把 NULL 悄悄变成 0 号用户，
    那是一个真实存在的用户 id 的风险（0 一般不出现，但"一般不"不是保证）。
    """
    raw_owner = row["owner_user_id"]
    return HistoryRecord(
        workflow_id=row["workflow_id"],
        question=row["question"],
        status=row["status"],
        created_at=_from_db_time(row["created_at"]),
        finished_at=_from_db_time(row["finished_at"]),
        has_report=bool(row["has_report"]),
        llm_used=bool(row["llm_used"]),
        report_text=row["report_text"],
        error_code=row["error_code"],
        archived_at=_from_db_time(row["archived_at"]),
        owner_user_id=int(raw_owner) if raw_owner is not None else None,
        owner_username=row["owner_username"],
    )


@lru_cache(maxsize=1)
def get_history_store() -> HistoryStore | None:
    """按当前配置拿归档库；`settings.db_path` 为空字符串 → `None`（**归档关闭**）。

    `None` 是正常状态而不是错误：用户可以用 `DB_PATH=` 明确表示"不要归档"，
    工作流照常跑。所以调用方的写法是：

        store = get_history_store()
        if store is None:
            return          # 归档关闭，什么都不做

    没缓存"库能不能用"这个结论 —— 构造 `HistoryStore` 不碰磁盘，
    真正的失败会在第一次 `archive()` 时以 `HistoryStoreError` 出现。
    """
    settings = get_settings()
    if not settings.db_path.strip():
        return None
    return HistoryStore(settings.db_file)


def reset_history_store_cache() -> None:
    """清 `get_history_store()` 的缓存，给测试用。

    改了 `DB_PATH` / `db_path` 之后必须清一次，否则拿到的是上一个配置的库
    （`lru_cache` 会一直返回同一个实例，而它记着旧路径）。
    """
    get_history_store.cache_clear()
