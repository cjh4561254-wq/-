"""`app/data/history.py`（SQLite 归档库）的测试。

范围与纪律
----------
- **全部用 `tmp_path`**，绝不碰仓库里的 `data/db/app.db`：那是真跑归档时写的地方，
  测试碰它既会搅乱本机的历史数据，也会让"跑一次测试"变成有副作用的事。
- 每个用例对应 task-31 点名的一条技术点：
  短连接 / 写锁（并发）/ UPSERT 幂等 / UTC-ISO 字符串 / 幂等建表 / 版本不符抛错 /
  异常不吞 / 空 `db_path` 关闭归档。
- `DB_PATH` 会被 `monkeypatch` 清干净并在测试后还原（`monkeypatch` 自动回滚），
  否则本机真实环境变量会渗进来，出现"我的机器绿、用户机器红"。
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import config
from app.config import PROJECT_ROOT, Settings
from app.data.history import (
    HISTORY_SCHEMA_VERSION,
    HistoryRecord,
    HistoryStore,
    HistoryStoreError,
    get_history_store,
    reset_history_store_cache,
)

#: 表结构的**独立**副本：测试自己建一遍，用来验证"代码建出来的表"与这里的期望一致，
#: 也用来手工造"版本不符的老库"。故意不 import history 里的私有 SQL 常量 ——
#: 那样就变成"拿实现验证实现"了。
_CREATE_TABLE_SQL = """
CREATE TABLE workflow_history (
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
    archived_at    TEXT NOT NULL
)
"""


@pytest.fixture(autouse=True)
def _isolate_db_path(monkeypatch: pytest.MonkeyPatch):
    """把 `DB_PATH` 清空、把两个 `lru_cache` 前后各清一次。

    配置缓存（`config.get_settings`）与归档库缓存（`get_history_store`）都是
    `lru_cache`：不在用例前后清，就会拿到上一个用例的库（一个 `tmp_path`，
    已经被 pytest 删掉了），报出来的是"文件不存在"，而不是真正的失败原因。
    """
    monkeypatch.delenv("DB_PATH", raising=False)
    config.reset_settings_cache()
    reset_history_store_cache()
    yield
    config.reset_settings_cache()
    reset_history_store_cache()


def _record(workflow_id: str = "a" * 32, **overrides: object) -> HistoryRecord:
    """一条"正常跑完、有报告"的记录；用 `overrides` 改任意字段。"""
    base: dict[str, object] = {
        "workflow_id": workflow_id,
        "question": "各地区的销售额汇总",
        "status": "success",
        "created_at": datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc),
        "finished_at": datetime(2026, 10, 14, 9, 0, 5, tzinfo=timezone.utc),
        "has_report": True,
        "llm_used": False,
        "report_text": '{"title": "分析报告：各地区的销售额汇总"}',
        "error_code": None,
        "archived_at": datetime(2026, 10, 14, 9, 0, 6, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return HistoryRecord(**base)  # type: ignore[arg-type]


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


# ---- ① 逐字段往返（含时区） -------------------------------------------------


def test_a_fresh_store_round_trips_every_field(tmp_path: Path) -> None:
    """写进去再读出来，**每个字段**都要相等（`dataclass` 相等看不出发是哪个字段变了）。"""
    store = HistoryStore(tmp_path / "app.db")
    record = _record()

    store.archive(record)

    got = store.get(record.workflow_id)
    assert got is not None
    assert got == record
    for field in dataclasses.fields(HistoryRecord):
        assert getattr(got, field.name) == getattr(record, field.name), field.name
    # 两个布尔字段在库里是 INTEGER，读回来必须是 bool（否则 `is True` 会糊掉）
    assert got.has_report is True
    assert got.llm_used is False


def test_aware_timestamps_land_in_utc_and_keep_the_same_instant(tmp_path: Path) -> None:
    """带 +08:00 的时间：存进去是 UTC 字符串，读回来是同一个**时刻**。

    这一条钉的是"统一折算成 UTC"：如果只做 `_as_utc()`（朴素补 UTC、带时区原样
    返回），库里就会混着 `+08:00` 与 `+00:00`，而 `ORDER BY` 比的是字符串 ——
    字典序与时间序会不一致。
    """
    store = HistoryStore(tmp_path / "app.db")
    shanghai = timezone(timedelta(hours=8))
    record = _record(
        created_at=datetime(2026, 10, 14, 17, 0, 0, tzinfo=shanghai),
        finished_at=datetime(2026, 10, 14, 17, 0, 5, tzinfo=shanghai),
        archived_at=datetime(2026, 10, 14, 17, 0, 6, tzinfo=shanghai),
    )

    store.archive(record)

    got = store.get(record.workflow_id)
    assert got is not None
    assert got.created_at == record.created_at          # 同一时刻（datetime 比的是 instant）
    assert got.created_at.utcoffset() == timedelta(0)   # 但落在 UTC
    assert got.created_at.hour == 9                     # 17:00+08:00 == 09:00Z
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT created_at FROM workflow_history").fetchone()
    assert row[0] == "2026-10-14T09:00:00+00:00"


def test_naive_timestamps_are_treated_as_utc(tmp_path: Path) -> None:
    """朴素时间当 UTC（和 `routes_workflows._as_utc` 同款），读回来必须带时区。"""
    store = HistoryStore(tmp_path / "app.db")
    record = _record(created_at=datetime(2026, 10, 14, 9, 0, 0))

    store.archive(record)

    got = store.get(record.workflow_id)
    assert got is not None
    assert got.created_at == datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc)
    assert got.created_at.tzinfo is not None


def test_nullable_fields_survive_the_round_trip(tmp_path: Path) -> None:
    """未跑完/失败的工作流：`finished_at` / `report_text` / `error_code` 可以是 None。"""
    store = HistoryStore(tmp_path / "app.db")
    record = _record(
        workflow_id="f" * 32,
        status="failed",
        finished_at=None,
        has_report=False,
        report_text=None,
        error_code="workflow_timeout",
    )

    store.archive(record)

    got = store.get("f" * 32)
    assert got is not None
    assert got.finished_at is None
    assert got.report_text is None
    assert got.error_code == "workflow_timeout"
    assert got.has_report is False


# ---- ② 重复归档无害（UPSERT） ----------------------------------------------


def test_archiving_the_same_workflow_twice_overwrites_one_row(tmp_path: Path) -> None:
    """同一条工作流归档两次：不报错、不留第二行，**第二次的值胜出**。

    真实场景：orchestrator 跑完归档一次（success），之后轮询接口又发现它超时并
    改判 failed，于是再归档一次。
    """
    store = HistoryStore(tmp_path / "app.db")
    store.archive(_record())

    store.archive(
        _record(
            status="failed",
            has_report=False,
            report_text=None,
            error_code="workflow_timeout",
            finished_at=datetime(2026, 10, 14, 9, 3, 0, tzinfo=timezone.utc),
        )
    )

    assert store.count() == 1
    got = store.get("a" * 32)
    assert got is not None
    assert got.status == "failed"
    assert got.error_code == "workflow_timeout"
    assert got.has_report is False
    assert got.report_text is None


def test_two_different_workflows_are_two_rows(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "app.db")
    store.archive(_record(workflow_id="1" * 32, question="问题一"))
    store.archive(_record(workflow_id="2" * 32, question="问题二"))

    assert store.count() == 2
    assert store.get("1" * 32).question == "问题一"  # type: ignore[union-attr]
    assert store.get("2" * 32).question == "问题二"  # type: ignore[union-attr]


# ---- ③ 排序与分页 ----------------------------------------------------------


def test_list_recent_sorts_by_finished_at_desc_with_none_last(tmp_path: Path) -> None:
    """`finished_at` 为 None（没跑完）的排在最后，其余按完成时间倒序。"""
    store = HistoryStore(tmp_path / "app.db")
    t = datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc)
    store.archive(_record(workflow_id="1" * 32, question="早", finished_at=t))
    store.archive(
        _record(workflow_id="2" * 32, question="晚", finished_at=t + timedelta(minutes=10))
    )
    store.archive(_record(workflow_id="3" * 32, question="没跑完", finished_at=None))

    got = store.list_recent()

    assert [r.question for r in got] == ["晚", "早", "没跑完"]


def test_list_recent_breaks_ties_with_created_at_desc(tmp_path: Path) -> None:
    """同秒完成的两条要有确定顺序：再按 `created_at` 倒序（否则分页会漏/重）。"""
    store = HistoryStore(tmp_path / "app.db")
    same_finished = datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc)
    store.archive(
        _record(
            workflow_id="1" * 32,
            question="先创建",
            created_at=same_finished - timedelta(minutes=5),
            finished_at=same_finished,
        )
    )
    store.archive(
        _record(
            workflow_id="2" * 32,
            question="后创建",
            created_at=same_finished - timedelta(minutes=1),
            finished_at=same_finished,
        )
    )

    assert [r.question for r in store.list_recent()] == ["后创建", "先创建"]


def test_list_recent_paginates(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "app.db")
    base = datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc)
    for index in range(5):
        store.archive(
            _record(
                workflow_id=f"{index:032x}",
                question=f"问题{index}",
                finished_at=base + timedelta(minutes=index),
            )
        )

    assert [r.question for r in store.list_recent(limit=2)] == ["问题4", "问题3"]
    assert [r.question for r in store.list_recent(limit=2, offset=2)] == ["问题2", "问题1"]
    assert [r.question for r in store.list_recent(limit=2, offset=4)] == ["问题0"]
    assert store.list_recent(limit=2, offset=5) == []
    assert store.list_recent(limit=0) == []
    assert len(store.list_recent()) == 5


def test_list_recent_rejects_negative_limit_or_offset(tmp_path: Path) -> None:
    """负数是编程错误：SQLite 会把 `LIMIT -1` 当"不限制"，静默返回全部更难查。"""
    store = HistoryStore(tmp_path / "app.db")
    with pytest.raises(ValueError):
        store.list_recent(limit=-1)
    with pytest.raises(ValueError):
        store.list_recent(offset=-1)


# ---- ④ 幂等建表 / 父目录自动创建 / 读路径自动建表 --------------------------


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "app.db")
    for _ in range(5):
        store.ensure_schema()
    assert _table_names(store.path) == {"workflow_history"}

    store.archive(_record())
    store.ensure_schema()  # 已有数据之后再建一次：不能报错、也不能清空
    assert store.count() == 1


def test_parent_directories_are_created_on_demand(tmp_path: Path) -> None:
    """`data/db/` 这种目录在全新克隆的仓库里并不存在（只有 .gitkeep）。"""
    store = HistoryStore(tmp_path / "deep" / "nested" / "app.db")
    assert not store.path.parent.exists()

    store.archive(_record())

    assert store.path.parent.is_dir()
    assert store.path.is_file()


def test_reads_work_without_calling_ensure_schema_first(tmp_path: Path) -> None:
    """调用方不该被迫记得先 `ensure_schema()`：四个操作内部都要自己确保表存在。"""
    store = HistoryStore(tmp_path / "app.db")

    assert store.count() == 0
    assert store.get("a" * 32) is None
    assert store.list_recent() == []

    store.archive(_record())

    assert store.count() == 1
    assert store.get("a" * 32) is not None


# ---- ⑤ 出错时抛 HistoryStoreError（不吞） ---------------------------------


def test_a_directory_as_db_path_raises_history_store_error(tmp_path: Path) -> None:
    """`DB_PATH` 指到一个目录：建表和归档都必须抛 `HistoryStoreError`。"""
    directory = tmp_path / "not_a_file"
    directory.mkdir()
    store = HistoryStore(directory)

    with pytest.raises(HistoryStoreError):
        store.ensure_schema()
    with pytest.raises(HistoryStoreError):
        store.archive(_record())


def test_a_file_blocking_the_parent_directory_raises_history_store_error(
    tmp_path: Path,
) -> None:
    """父目录位置被一个**文件**占着 → `mkdir` 失败，也要变成 `HistoryStoreError`。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    store = HistoryStore(blocker / "app.db")

    with pytest.raises(HistoryStoreError):
        store.archive(_record())


def test_schema_version_mismatch_raises_and_keeps_the_data(tmp_path: Path) -> None:
    """版本不符：抛错、**不迁移、不删表**，老数据一个字节都不能少。"""
    db_path = tmp_path / "app.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(
            "INSERT INTO workflow_history (workflow_id, question, status, created_at,"
            " finished_at, has_report, llm_used, report_text, error_code,"
            " schema_version, archived_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("b" * 32, "老数据", "success", "2026-10-14T09:00:00+00:00",
             "2026-10-14T09:00:05+00:00", 1, 0, None, None, 99,
             "2026-10-14T09:00:06+00:00"),
        )
    store = HistoryStore(db_path)

    with pytest.raises(HistoryStoreError) as excinfo:
        store.ensure_schema()
    assert "版本不符" in str(excinfo.value)
    with pytest.raises(HistoryStoreError):
        store.get("b" * 32)
    with pytest.raises(HistoryStoreError):
        store.archive(_record(workflow_id="c" * 32))

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT workflow_id, question, schema_version FROM workflow_history").fetchall()
    assert rows == [("b" * 32, "老数据", 99)]


def test_the_schema_version_written_by_the_code_is_the_constant(tmp_path: Path) -> None:
    """写进去的行必须带当前版本号 —— 否则下一次升级会把好数据判成老数据。"""
    store = HistoryStore(tmp_path / "app.db")
    store.archive(_record())

    with sqlite3.connect(store.path) as conn:
        version = conn.execute("SELECT schema_version FROM workflow_history").fetchone()[0]
    assert version == HISTORY_SCHEMA_VERSION


# ---- 连接层面的两条约定（短连接 / busy_timeout / 不开 WAL） -----------------


def test_every_connection_sets_busy_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PRAGMA busy_timeout` 是**连接级**的：每条新连接都必须设一遍。

    直接读 `PRAGMA busy_timeout` 是读不出来的（那是"当前这条连接"的值，
    而实现里的连接用完就关了），所以这里拦在 `sqlite3.connect` 上记账。
    """
    seen: list[str] = []

    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: object):  # type: ignore[override]
            if sql.lstrip().upper().startswith("PRAGMA"):
                seen.append(sql)
            return super().execute(sql, *args)  # type: ignore[arg-type]

    real_connect = sqlite3.connect

    def recording_connect(database: object, *args: object, **kwargs: object):
        kwargs["factory"] = RecordingConnection
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", recording_connect)

    store = HistoryStore(tmp_path / "app.db")
    store.archive(_record())
    store.get("a" * 32)

    assert any("busy_timeout = 5000" in sql for sql in seen)
    assert len(seen) >= 2  # 每条连接各设一次，不是只在第一条上设


def test_journal_mode_stays_default_and_leaves_no_extra_files(tmp_path: Path) -> None:
    """不开 WAL：库旁边只有 `app.db` 一个文件，且 journal_mode 是默认的 delete。"""
    store = HistoryStore(tmp_path / "app.db")
    store.archive(_record())

    with sqlite3.connect(store.path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.db"]


# ---- ⑦ 100 个后台线程并发归档（真实形态） ---------------------------------


def test_a_hundred_threads_archive_concurrently(tmp_path: Path) -> None:
    """100 条工作流 = 100 个后台线程（`app/core/background.py` 的真实形态）。

    这条钉的是**短连接**这条设计：如果实现改成"共享一个长连接"，sqlite3 会因为
    `check_same_thread=True` 直接抛 `ProgrammingError`，这里就会红。
    （反过来说清楚：去掉那把 `_WRITE_LOCK` 这条测试**未必**会红 —— 每条连接都设了
    `busy_timeout`，SQLite 自己也会串行化写；锁是"进程内不做无谓争抢"的优化，
    不是这条测试的靶子。别拿它的绿去证明锁还在。）
    """
    store = HistoryStore(tmp_path / "app.db")
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            store.archive(_record(workflow_id=f"{index:032x}", question=f"问题 {index}"))
        except BaseException as exc:  # noqa: BLE001 - 线程里的异常必须带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert store.count() == 100
    assert store.get(f"{7:032x}").question == "问题 7"  # type: ignore[union-attr]


# ---- ⑥ 空 db_path 关闭归档 + 配置接线 -------------------------------------


def test_get_history_store_returns_none_when_db_path_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DB_PATH=` 是**明确关闭归档**，不是错误：返回 None，工作流照常跑。"""
    monkeypatch.setenv("DB_PATH", "")
    config.reset_settings_cache()
    reset_history_store_cache()

    assert get_history_store() is None


def test_get_history_store_returns_none_for_whitespace_only_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有空格也算"没配"：否则会拼出一个叫 `"   "` 的文件名。"""
    monkeypatch.setenv("DB_PATH", "   ")
    config.reset_settings_cache()
    reset_history_store_cache()

    assert get_history_store() is None


def test_get_history_store_uses_the_configured_path_and_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认走 `settings.db_file`；`lru_cache` 保证同一配置只造一个实例。"""
    configured = tmp_path / "cfg.db"
    monkeypatch.setenv("DB_PATH", str(configured))
    config.reset_settings_cache()
    reset_history_store_cache()

    store = get_history_store()
    assert store is not None
    assert store.path == configured
    assert get_history_store() is store

    # 改了配置就必须清缓存，否则拿到的是记着旧路径的那个实例
    other = tmp_path / "other.db"
    monkeypatch.setenv("DB_PATH", str(other))
    config.reset_settings_cache()
    reset_history_store_cache()
    assert get_history_store().path == other  # type: ignore[union-attr]


def test_db_path_defaults_inside_the_repo_and_db_file_resolves_it() -> None:
    """第 16 个字段：默认 `data/db/app.db`；`db_file` 把相对路径折成绝对路径。"""
    settings = Settings()
    assert settings.db_path == "data/db/app.db"
    assert settings.db_file == PROJECT_ROOT / "data" / "db" / "app.db"

    absolute = Settings(db_path=r"C:\somewhere\else\app.db")
    assert absolute.db_file == Path(r"C:\somewhere\else\app.db")

    # 空串是"关闭归档"，此时 db_file 会退回项目根 —— 记在这里，免得将来有人
    # 直接拿它去 sqlite3.connect()（get_history_store() 已经挡住了这一层）。
    assert Settings(db_path="").db_file == PROJECT_ROOT


def test_the_store_module_stays_on_the_standard_library() -> None:
    """零新依赖是硬约束：这个模块只许 import 标准库 + `app`。

    这里用 `ast` 解析**真正的 import 语句**，而不是在源码文本里搜字符串 ——
    模块 docstring 里正好写了"为什么不用 SQLAlchemy"，按文本搜会被自己的注释骗到
    （这个坑在 D6 前端那次已经踩过一次：`resp.ok` 写在注释里，断言就红了）。
    """
    import ast

    source = (PROJECT_ROOT / "app" / "data" / "history.py").read_text(encoding="utf-8")

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported == {
        "__future__",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "functools",
        "pathlib",
        "sqlite3",
        "threading",
        "app",
    }
