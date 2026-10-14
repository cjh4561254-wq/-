"""历史归档两个只读接口的契约测试（接口 9 `GET /workflows/history`、接口 10 详情）。

两条硬证据（task-32 的验收点，改动这两个接口时先看这里）
-----------------------------------------------------
① **路由顺序**：`/api/workflows/history` 与 `/api/workflows/{workflow_id}` 的路径形状
   一模一样，FastAPI 按**声明顺序**匹配。历史接口写在后面就会被后者接走、
   `workflow_id="history"` → 404。`test_the_history_path_is_not_swallowed_by_the_workflow_route`
   同时断言两个方向：`/api/workflows/history` 是 200 且 `ok=true`，
   而一个不存在的 32 位 id 仍然是 404「工作流不存在」—— 两条分支必须能分开。

② **禁止复活**：`registry.clear()` 之后，`GET /api/workflows/{wid}` 仍然 404，
   但 `GET /api/workflows/history` 里**还能看到**这条（这正是"重启后历史还在"的语义）。
   断言里显式检查 `registry.count()` 在请求前后不变：只要有人加了
   "库里没有就去内存里捞一条"的 fallback，这条立刻红（§8.3.1 明令禁止）。

测试用的库永远在 `tmp_path`，绝不碰仓库里的 `data/db/app.db`。
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.core import archive, registry
from app.data import history
from app.main import app
from app.schemas_workflow import (
    FinalReport,
    ReportSection,
    WorkflowState,
    WorkflowStatus,
)

WORKFLOW_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """归档库、上传目录都指到临时目录，并清掉两层缓存（进出各一次）。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db" / "app.db"))
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
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


@pytest.fixture
def client():
    """带 lifespan 的测试客户端。

    必须写成 `with TestClient(app) as client:` —— 模块级实例不跑 lifespan，
    启动时的建表（`app/main.py` 的 `_prepare_history_store`）就被跳过了。
    """
    with TestClient(app) as test_client:
        yield test_client


def _make_report() -> FinalReport:
    return FinalReport(
        report_id="deadbeef",
        title="销售额按地区分析",
        summary="华南的销售额明显高于其他地区。",
        sections=[ReportSection(heading="总体情况", text="总销售额为 1.0 元。", cites=["T1"])],
        citations=["T1"],
        generated_at=datetime.now(timezone.utc),
        llm_used=False,
    )


def _state(**overrides) -> WorkflowState:
    fields: dict = {
        "workflow_id": WORKFLOW_ID,
        "dataset_id": "d" * 32,
        "question": "按地区看销售额",
        "created_at": datetime.now(timezone.utc),
    }
    fields.update(overrides)
    return WorkflowState(**fields)


def _archived(**overrides) -> WorkflowState:
    """造一条终态并**走归档门面**写进库（不直接用 store，保持与生产同一条路径）。"""
    finished = overrides.pop("finished_at", datetime.now(timezone.utc))
    status = overrides.pop("status", WorkflowStatus.SUCCESS)
    state = _state(status=status, finished_at=finished, **overrides)
    assert archive.archive_if_terminal(state) is True
    return state


# ---------------- ① 路由顺序 ----------------


def test_the_history_path_is_not_swallowed_by_the_workflow_route(client) -> None:
    """`/api/workflows/history` 必须 200 且 `ok=true`，不能变成 404「工作流不存在」。"""
    resp = client.get("/api/workflows/history")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    # 形状是列表（items/enabled），而不是被 `get_workflow` 接走后的 404 外壳。
    assert set(body["data"]) == {"items", "total", "limit", "offset", "enabled"}

    # 对照组：一个真的不存在的 workflow_id 仍然是 404，两条分支分得开。
    missing = client.get(f"/api/workflows/{WORKFLOW_ID}")
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "工作流不存在"


# ---------------- ② 禁止复活（内存是内存，历史是历史） ----------------


def test_history_survives_registry_clear_but_the_live_endpoint_does_not(client) -> None:
    """`registry.clear()` 之后：实时接口 404，历史接口仍能看到这条记录。"""
    report = _make_report()
    state = _state(
        status=WorkflowStatus.SUCCESS,
        finished_at=datetime.now(timezone.utc),
        report=report,
        has_report=True,
    )
    registry.create(state)
    assert archive.archive_if_terminal(state) is True

    registry.clear()
    count_before = registry.count()
    assert count_before == 0

    live = client.get(f"/api/workflows/{WORKFLOW_ID}")
    assert live.status_code == 404
    assert registry.count() == count_before, "查状态接口不许把对象复活回注册表"

    history_list = client.get("/api/workflows/history")
    assert history_list.status_code == 200
    items = history_list.json()["data"]["items"]
    assert [item["workflow_id"] for item in items] == [WORKFLOW_ID]
    assert registry.count() == count_before, "历史列表只读库，不许把对象捞回内存"
    assert registry.get(WORKFLOW_ID) is None

    detail = client.get(f"/api/workflows/history/{WORKFLOW_ID}")
    assert detail.status_code == 200
    assert detail.json()["data"]["item"]["workflow_id"] == WORKFLOW_ID
    assert registry.count() == count_before, "历史详情只读库，不许把对象捞回内存"


def test_detail_does_not_resurrect_a_workflow_that_only_exists_in_memory(client) -> None:
    """只在内存里、从未归档的工作流：详情接口必须 404，而不是"顺手从 registry 拿一份"。"""
    registry.create(_state(status=WorkflowStatus.PENDING))
    count_before = registry.count()

    resp = client.get(f"/api/workflows/history/{WORKFLOW_ID}")

    assert resp.status_code == 404
    assert resp.json()["error"]["message"] == "没有归档记录"
    assert registry.count() == count_before


# ---------------- 列表：空库、排序、分页 ----------------


def test_an_empty_history_is_not_an_error(client) -> None:
    """空库 + 归档开启 → 200 / `enabled=true` / 空列表 / `total=0`（不是 404）。"""
    body = client.get("/api/workflows/history").json()

    assert body["ok"] is True
    data = body["data"]
    assert data["enabled"] is True
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


def test_list_is_newest_first_and_paginates(client) -> None:
    base = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
    ids = [f"{index:032x}" for index in (1, 2, 3)]
    for index, workflow_id in enumerate(ids):
        _archived(
            workflow_id=workflow_id,
            finished_at=base + timedelta(minutes=index),
        )

    page = client.get("/api/workflows/history").json()["data"]
    assert [item["workflow_id"] for item in page["items"]] == list(reversed(ids))
    assert page["total"] == 3, "total 是全库总数，不是本页条数"

    first = client.get("/api/workflows/history?limit=2&offset=0").json()["data"]
    second = client.get("/api/workflows/history?limit=2&offset=2").json()["data"]
    assert [item["workflow_id"] for item in first["items"]] == [ids[2], ids[1]]
    assert first["total"] == 3
    assert first["limit"] == 2
    assert [item["workflow_id"] for item in second["items"]] == [ids[0]]


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_paging_parameters_are_validated(client, query: str) -> None:
    """`limit`/`offset` 越界由 FastAPI 的 Query 约束挡下（422），不是静默截断。"""
    resp = client.get(f"/api/workflows/history?{query}")

    assert resp.status_code == 422
    assert resp.json()["ok"] is False


# ---------------- 详情：还原报告 / 404 ----------------


def test_detail_restores_the_archived_report(client) -> None:
    report = _make_report()
    _archived(
        status=WorkflowStatus.SUCCESS_WITH_WARNINGS,
        report=report,
        has_report=True,
    )

    body = client.get(f"/api/workflows/history/{WORKFLOW_ID}").json()

    assert body["ok"] is True
    data = body["data"]
    assert data["item"]["status"] == "SUCCESS_WITH_WARNINGS"
    assert data["item"]["has_report"] is True
    assert data["item"]["llm_used"] is False
    assert data["item"]["question"] == "按地区看销售额"
    # 报告是**当时那一份**：从 report_text 还原出来，逐字段相等。
    restored = FinalReport.model_validate(data["report"])
    assert restored == report
    assert restored.summary == report.summary
    assert [section.heading for section in restored.sections] == ["总体情况"]


def test_detail_returns_null_report_when_nothing_was_archived_for_it(client) -> None:
    """跑了但没出报告的工作流：item 在、`report` 是 null（不是 404）。"""
    _archived(status=WorkflowStatus.FAILED)

    data = client.get(f"/api/workflows/history/{WORKFLOW_ID}").json()["data"]

    assert data["item"]["has_report"] is False
    assert data["report"] is None


def test_detail_404_for_an_unknown_workflow(client) -> None:
    resp = client.get(f"/api/workflows/history/{WORKFLOW_ID}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert resp.json()["error"]["message"] == "没有归档记录"


# ---------------- 归档关闭（`DB_PATH=""`） ----------------


def test_history_is_disabled_but_still_200_when_db_path_is_empty(client, monkeypatch) -> None:
    """归档被关掉不是错误：`enabled=false` + 200（对比"报告还没生成"是 400）。"""
    _archived()  # 先写一条，证明"关掉"是配置说了算，不是库空了
    monkeypatch.setenv("DB_PATH", "")
    config.reset_settings_cache()
    history.reset_history_store_cache()

    resp = client.get("/api/workflows/history")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is False
    assert data["items"] == []
    assert data["total"] == 0

    detail = client.get(f"/api/workflows/history/{WORKFLOW_ID}")
    assert detail.status_code == 404


# ---------------- 惰性超时也要归档（第二条归档路径） ----------------


def test_the_lazy_timeout_path_archives_the_workflow_too(client) -> None:
    """轮询判超时的那条路径也要归档 —— 否则"卡住的工作流"永远进不了历史。

    为什么这条值得写：终态有两条到达路径。正常跑完走 orchestrator 的 `finally`
    （已被 `test_orchestrator_early_failure_is_archived_at_the_exit` 钉住）；而
    "心跳过期被轮询判超时"是**接口 6 现算出来的终态**，根本没有 orchestrator 的收口
    可走 —— 只有 `_expire_if_stale()` 末尾那次调用能把它写进库。
    `HistoryStore.archive()` 是 UPSERT，所以同一条被归档两次也不会长出两行。

    判据同时钉住 HTTP 语义：超时是**工作流失败**，不是请求失败，所以仍是 200。
    """
    stale = _state(
        status=WorkflowStatus.EXECUTING,
        heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=3600),
    )
    registry.create(stale)
    assert registry.count() == 1

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}")
    assert resp.status_code == 200  # 超时也是 200（§7.0），不是 5xx
    data = resp.json()["data"]
    assert data["status"] == "FAILED"
    assert data["error"]["code"] == "WORKFLOW_TIMEOUT"

    listed = client.get("/api/workflows/history").json()["data"]
    assert listed["total"] == 1
    item = listed["items"][0]
    assert item["workflow_id"] == WORKFLOW_ID
    assert item["status"] == "FAILED"
    assert item["error_code"] == "WORKFLOW_TIMEOUT"
    assert item["has_report"] is False
    assert item["finished_at"] is not None  # 超时分支自己写了 finished_at
