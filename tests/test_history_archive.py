"""归档门面的单元测试（`app/core/archive.py`）：终态才写、失败不闹事、字段逐条对得上。

为什么这些用例都要 `tmp_path` + `DB_PATH`
----------------------------------------
归档库是**真实的 SQLite 文件**，`D-16` 那条教训在这里同样适用：测试不许在仓库里
留下 `data/db/app.db`（用户自己的历史记录），也不许依赖上一次运行残留的库。
所以夹具把 `DB_PATH` 指到 `tmp_path`，并把 `config` / `history` 两层缓存都清掉 ——
缓存的单例不清，"改了环境变量却没生效"会以"上一个用例的库还在"的形式出现。

为什么"归档失败要吞掉"是**契约**而不是实现细节
--------------------------------------------
归档是旁路：磁盘满、目录只读、库被占用，都不该影响用户已经算完的结果。
`archive_if_terminal()` 的返回值是"这次写没写进去"，调用方（orchestrator、
轮询接口）拿它决定要不要记日志，而**不是**拿它决定要不要报错。所以这里的
失败用例断言的是"返回 False + 有一条 warning"，而不是"抛异常"。
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app import config
from app.agents import orchestrator
from app.core import archive, registry
from app.data import history
from app.data.history import HistoryStoreError
from app.schemas import ApiError, ErrorCode
from app.schemas_workflow import (
    FinalReport,
    ReportSection,
    WorkflowState,
    WorkflowStatus,
)

WORKFLOW_ID = "0123456789abcdef0123456789abcdef"

# 非终态：这些状态下**一个字都不该写库**。
NON_TERMINAL_STATUSES = [
    WorkflowStatus.PENDING,
    WorkflowStatus.PROFILING,
    WorkflowStatus.EXECUTING,
    WorkflowStatus.VALIDATING,
]

TERMINAL_STATUSES = [
    WorkflowStatus.SUCCESS,
    WorkflowStatus.SUCCESS_WITH_WARNINGS,
    WorkflowStatus.FAILED,
]


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """把归档库指到临时目录，并清掉两层缓存（进出各一次）。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db" / "app.db"))
    config.reset_settings_cache()
    history.reset_history_store_cache()
    registry.clear()
    yield
    registry.clear()
    history.reset_history_store_cache()
    config.reset_settings_cache()


def teardown_function() -> None:
    """用例结束立刻清全局状态（D-16）：注册表与 store 单例都是进程内全局的。"""
    registry.clear()
    history.reset_history_store_cache()


def _store() -> history.HistoryStore:
    store = history.get_history_store()
    assert store is not None, "夹具里 DB_PATH 非空，store 不该是 None"
    return store


def _make_report(llm_used: bool = False) -> FinalReport:
    return FinalReport(
        report_id="deadbeef",
        title="销售额按地区分析",
        summary="华南的销售额明显高于其他地区。",
        sections=[ReportSection(heading="总体情况", text="总销售额为 1.0 元。", cites=["T1"])],
        citations=["T1"],
        generated_at=datetime.now(timezone.utc),
        llm_used=llm_used,
    )


def _state(**overrides) -> WorkflowState:
    """造一条状态，默认"刚创建、还没跑"。用例只覆盖自己关心的字段。"""
    fields: dict = {
        "workflow_id": WORKFLOW_ID,
        "dataset_id": "d" * 32,
        "question": "按地区看销售额",
        "created_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return WorkflowState(**fields)


# ---------------- 规则 1：非终态一个字都不写 ----------------


@pytest.mark.parametrize("status", NON_TERMINAL_STATUSES)
def test_a_running_workflow_is_not_archived(status: WorkflowStatus) -> None:
    state = _state(status=status)

    assert archive.archive_if_terminal(state) is False
    assert _store().count() == 0, "非终态不该在库里留下任何记录"


# ---------------- 规则 2：每个终态都归档 ----------------


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_every_terminal_status_is_archived(status: WorkflowStatus) -> None:
    state = _state(status=status, finished_at=datetime.now(timezone.utc))

    assert archive.archive_if_terminal(state) is True

    record = _store().get(WORKFLOW_ID)
    assert record is not None
    assert record.status == status.value
    assert _store().count() == 1


def test_archiving_the_same_workflow_twice_keeps_one_row() -> None:
    """幂等：orchestrator 归档一次、之后惰性超时又归档一次，不能长出两条。"""
    state = _state(status=WorkflowStatus.SUCCESS, finished_at=datetime.now(timezone.utc))

    assert archive.archive_if_terminal(state) is True
    assert archive.archive_if_terminal(state) is True

    assert _store().count() == 1


# ---------------- 规则 3：归档关闭 = 静默返回 False ----------------


def test_archive_is_off_when_db_path_is_empty(monkeypatch) -> None:
    """`DB_PATH=""` = 用户主动关掉归档：不是错误，也不许抛异常。"""
    monkeypatch.setenv("DB_PATH", "")
    config.reset_settings_cache()
    history.reset_history_store_cache()

    state = _state(status=WorkflowStatus.SUCCESS, finished_at=datetime.now(timezone.utc))

    assert history.get_history_store() is None
    assert archive.archive_if_terminal(state) is False


# ---------------- 规则 4：任何异常都不许往外抛 ----------------


def test_store_failure_is_swallowed_and_logged(monkeypatch, caplog) -> None:
    """写库失败（这里是 monkeypatch 出来的）→ False + warning + 不抛。"""

    def _boom(self, record) -> None:  # noqa: ANN001 —— 只为复现"写不进去"
        raise HistoryStoreError("磁盘满了")

    monkeypatch.setattr(history.HistoryStore, "archive", _boom)
    state = _state(status=WorkflowStatus.SUCCESS, finished_at=datetime.now(timezone.utc))

    with caplog.at_level(logging.WARNING, logger="app.core.archive"):
        result = archive.archive_if_terminal(state)

    assert result is False
    assert _store().count() == 0
    messages = [record.getMessage() for record in caplog.records]
    assert any("归档失败" in message for message in messages), messages
    assert any(WORKFLOW_ID in message for message in messages), "日志要带 workflow_id 才查得动"
    assert any("HistoryStoreError" in message for message in messages), "日志要带异常类型"


def test_unwritable_db_path_is_swallowed_and_logged(tmp_path, monkeypatch, caplog) -> None:
    """真实的写库失败：`DB_PATH` 指到一个**目录**，sqlite 打不开 → 仍然只记 warning。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path))
    config.reset_settings_cache()
    history.reset_history_store_cache()

    state = _state(status=WorkflowStatus.FAILED, finished_at=datetime.now(timezone.utc))

    with caplog.at_level(logging.WARNING, logger="app.core.archive"):
        result = archive.archive_if_terminal(state)

    assert result is False
    assert any("归档失败" in record.getMessage() for record in caplog.records)


# ---------------- 字段映射：逐条对得上 ----------------


def test_record_fields_match_the_state() -> None:
    created = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
    finished = created + timedelta(seconds=3)
    report = _make_report(llm_used=True)
    error = ApiError(code=ErrorCode.INTERNAL_ERROR, message="分析已完成，但生成报告时出错")
    state = _state(
        status=WorkflowStatus.SUCCESS_WITH_WARNINGS,
        created_at=created,
        finished_at=finished,
        report=report,
        has_report=True,  # 故意与 report 一致；下面另一条用例专门制造"不一致"
        error=error,
    )

    assert archive.archive_if_terminal(state) is True

    record = _store().get(WORKFLOW_ID)
    assert record is not None
    assert record.workflow_id == WORKFLOW_ID
    assert record.question == "按地区看销售额"
    assert record.status == "SUCCESS_WITH_WARNINGS"
    assert record.created_at == created
    assert record.finished_at == finished
    assert record.has_report is True
    assert record.llm_used is True
    assert record.report_text == report.model_dump_json()
    assert record.error_code == "INTERNAL_ERROR"
    assert record.archived_at.tzinfo is not None, "归档时间必须带时区，否则排序口径会飘"


def test_has_report_and_llm_used_come_from_the_report_not_the_flags() -> None:
    """派生值现算：`has_report=True` 但 `report is None` 时，记录必须是 False。

    这条钉的是"不抄可能没同步的字段"（与 `WorkflowStatusData.from_state()` 同一条纪律）。
    如果哪天有人把映射改成读 `state.has_report` / 另存一份 `llm_used`，这条会红。
    """
    state = _state(
        status=WorkflowStatus.SUCCESS,
        finished_at=datetime.now(timezone.utc),
        report=None,
        has_report=True,  # 故意不同步：库里不该出现这样一条"有报告"的记录
    )

    assert archive.archive_if_terminal(state) is True

    record = _store().get(WORKFLOW_ID)
    assert record is not None
    assert record.has_report is False
    assert record.llm_used is False
    assert record.report_text is None


def test_finished_at_falls_back_to_archived_at() -> None:
    """终态但 `finished_at` 为空时用 `archived_at` 兜底，**不许留 None**。

    历史列表按 `finished_at` 排序，留一个 NULL 会让这条记录掉到列表最后 ——
    用户看到的现象是"我刚跑完的那次不见了"，而库里其实有记录。
    """
    state = _state(status=WorkflowStatus.SUCCESS, finished_at=None)

    assert archive.archive_if_terminal(state) is True

    record = _store().get(WORKFLOW_ID)
    assert record is not None
    assert record.finished_at is not None
    assert record.finished_at == record.archived_at


def test_archiving_does_not_touch_the_state() -> None:
    """归档是只读 state 的旁路：写完不能改动 state 的任何字段。"""
    state = _state(status=WorkflowStatus.SUCCESS, finished_at=datetime.now(timezone.utc))
    before = state.model_dump()

    assert archive.archive_if_terminal(state) is True

    assert state.model_dump() == before


# ---------------- 集成：orchestrator 的收口（含早退分支） ----------------


def test_orchestrator_early_failure_is_archived_at_the_exit() -> None:
    """**收口在函数出口**的证据：走"数据集不在内存里"这条早退分支也会被归档。

    这条用例的价值在于覆盖"最不常走的那条失败路径"：如果归档是写在
    `_final_status` 的四个分支里（而不是 `run_workflow` 的 `finally`），
    这条分支根本走不到归档代码 —— 而它恰恰是真实用户最容易碰到的一种失败
    （服务重启后数据没了）。
    """
    state = _state(status=WorkflowStatus.EXECUTING)

    orchestrator.run_workflow(state)  # 内存里没有这个数据集 → 立刻落 FAILED 并 return

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None and state.error.code is ErrorCode.NOT_FOUND

    record = _store().get(WORKFLOW_ID)
    assert record is not None, "早退分支也必须被 finally 里的收口归档"
    assert record.status == "FAILED"
    assert record.error_code == "NOT_FOUND"
    assert record.finished_at is not None
