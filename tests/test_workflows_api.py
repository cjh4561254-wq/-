"""工作流三个查询接口的契约测试：接口 6 轮询 / 接口 7 报告 / 接口 8 图表。

三条实测教训（写在最前面，免得下一个人再踩一遍）
--------------------------------------------
① `TestClient(app)` **必须**写成 `with TestClient(app) as client:` —— 模块级实例不跑
   lifespan，启动时的初始化全被跳过，测试"过"了却什么都没验证（tests/test_health.py
   记过这条）。
② `registry` 是**进程内全局状态**：上一条用例留下的工作流会污染下一条，而且只在
   测试顺序变化时才暴露（D-16）。所以夹具进出都 `clear()`，另外单独写
   `teardown_function` 兜住"夹具退出之后才创建"的情况。
③ 除了最后一条端到端用例，这里的**状态都是手工造好登记进 registry 的**：
   接口的正确性（状态码、字段、分支）与分析链路跑不跑得通无关。这样测试既快又稳，
   不会因为队友正在改 `app/agents/` 而红。

为什么要单独钉"终态不被改判"（见 test_a_finished_workflow_is_never_re_judged）
--------------------------------------------------------------------------
接口 6 会做惰性超时检测，而一条 `SUCCESS` 的工作流跑完之后就不再刷新 `heartbeat_at`。
一旦实现把顺序写反（先判超时、再判终态），三分钟后的一次普通轮询会把它**改判成
FAILED**：结果明明是好的，接口却说失败了。这条用例就是钉住那个顺序的（验收标准②）。

为什么接口 8 的非文件名要点两遍（HTTP 一遍 + 直接调函数一遍）
--------------------------------------------------------
顶层 Web 框架和 HTTP 客户端都会先对 URL 里的 `.` / `..` 做规范化，所以
`charts/../../../etc/passwd` 有可能在**进到我们的函数之前**就被改写或拒绝 ——
那样测出来的是框架的行为，不是我们的防线。直接调 `get_chart()` 才能真正证明
"这段代码自己会拒绝非法文件名"（也是变异测试②的靶子）。
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api.routes_workflows import get_chart, router
from app.core import registry
from app.errors import AppError
from app.main import app
from app.schemas import ErrorCode
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    FinalReport,
    ReportSection,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
)

# 一条合法的工作流 id（32 位小写十六进制）与一个合法的图表文件名（12 位 + .png）。
WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
CHART_ID = "0123456789ab"
CHART_FILENAME = f"{CHART_ID}.png"

# 几个字节就够：这里测的是响应头与"有没有把文件送出来"，不是 PNG 本身能不能打开。
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-body"

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

TERMINAL_STATUS_NAMES = {"SUCCESS", "SUCCESS_WITH_WARNINGS", "FAILED"}

# 接口 8 必须拒掉的非法文件名。列表里同时给出"路径穿越"和"格式不对"两类：
#   前者是安全问题，后者是契约问题（12 位 hex 才能和 ChartResult.chart_id 对上）。
ILLEGAL_FILENAMES = [
    "../../../etc/passwd",
    "..\\..\\evil.png",
    "abc.png",  # 6 位，不是 12 位
    "0123456789abc.png",  # 13 位
    "0123456789AB.png",  # 大写不算合法 hex
    "0123456789ab.png.exe",  # 后缀不对
    "0123456789ab",  # 没有 .png
    "",
]


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """把配置指到临时目录、把全局状态清干净（进出各一次）。

    `MAX_WORKFLOW_SECONDS` 手动设成 60 秒而不是读 `.env`：测试不该因为用户改了一下
    `.env` 就变红或变绿（Lead 明确要求过）。下面造"很久没心跳"用的是 3600 秒前，
    在任何合理上限下都算超时。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    monkeypatch.setenv("MAX_WORKFLOW_SECONDS", "60")
    # 三个 LLM 变量清空：端到端那条用例必须走规则兜底，不能真的联网调模型。
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()
    registry.clear()
    yield
    registry.clear()
    config.reset_settings_cache()


def teardown_function() -> None:
    """每条用例之后再清一次。

    夹具的 `yield` 之后也会清，但那是"进入下一条用例之前"才生效；
    保留这个函数是因为 D-16 的教训：**全局状态必须在用例结束时立刻清掉**，
    否则最后一条用例留下的工作流会活到整个测试会话结束，谁也不知道它什么时候咬人。
    """
    registry.clear()


@pytest.fixture
def client():
    """带 lifespan 的测试客户端（见模块 docstring 第 ① 条）。"""
    with TestClient(app) as test_client:
        yield test_client


def _make_report() -> FinalReport:
    """造一份最小的合法报告，用于"报告已生成"的分支。"""
    now = datetime.now(timezone.utc)
    return FinalReport(
        report_id="deadbeef",
        title="销售额按地区分析",
        summary="华南的销售额明显高于其他地区。",
        sections=[
            ReportSection(heading="总体情况", text="总销售额为 1.0 元。", cites=["T1"])
        ],
        citations=["T1"],
        generated_at=now,
        llm_used=False,
    )


def _make_result() -> ToolResult:
    """造一条最小的工具结果，用来验证接口 7 会把 `results` 原样带出来。"""
    return ToolResult(
        task_id="T1",
        tool_name="group_aggregate",
        ok=True,
        data={"rows": [{"地区": "华南", "销售额_sum": 1.0}]},
        columns=["地区", "销售额_sum"],
        row_count=1,
        elapsed_ms=3,
        created_at=datetime.now(timezone.utc),
    )


def _register(**overrides) -> WorkflowState:
    """造一条状态并登记进 registry，默认是"刚创建、正在跑、心跳新鲜"。

    参数用 `**overrides` 而不是一堆位置参数：每个用例只关心自己要改的一两个字段，
    其余走默认值，读起来才知道"这条用例在测什么"。
    """
    now = datetime.now(timezone.utc)
    fields: dict = {
        "workflow_id": WORKFLOW_ID,
        "dataset_id": "d" * 32,
        "question": "按地区看销售额",
        "status": WorkflowStatus.EXECUTING,
        "step": "execution",
        "created_at": now - timedelta(seconds=10),
        "started_at": now - timedelta(seconds=10),
        "heartbeat_at": now,
    }
    fields.update(overrides)
    state = WorkflowState(**fields)
    registry.create(state)
    return state


def test_status_interface_returns_the_small_status_shape(client):
    """接口 6 返回精简状态：有 has_report，但**没有 report / results**。"""
    _register(
        status=WorkflowStatus.SUCCESS,
        step="done",
        finished_at=datetime.now(timezone.utc),
        report=_make_report(),
        results=[_make_result()],
    )

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None
    data = body["data"]
    assert data["workflow_id"] == WORKFLOW_ID
    assert data["status"] == "SUCCESS"
    assert data["step"] == "done"
    # has_report 是现场算的（state.report is not None），不是抄 state.has_report
    assert data["has_report"] is True
    # 这两个是轮询接口故意不返回的大字段（§7.1 第 2 条）
    assert "report" not in data
    assert "results" not in data


def test_status_interface_marks_a_stale_workflow_as_timed_out(client):
    """心跳太久没刷新 → 惰性判定超时：落 FAILED + WORKFLOW_TIMEOUT，但 HTTP 仍是 200。"""
    stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
    state = _register(status=WorkflowStatus.EXECUTING, heartbeat_at=stale)

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}")

    # 工作流超时不是"这次查询失败"：查询成功了，失败的是被查询的工作流（§7.0 表）
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "FAILED"
    assert data["error"]["code"] == "WORKFLOW_TIMEOUT"
    assert data["finished_at"] is not None
    # 状态是**就地**改在注册表里那个对象上的：不只这次响应好看，后续轮询也要一致
    assert state.status is WorkflowStatus.FAILED
    assert state.finished_at is not None
    assert registry.get(WORKFLOW_ID).status is WorkflowStatus.FAILED


@pytest.mark.parametrize(
    "terminal_status",
    [WorkflowStatus.SUCCESS, WorkflowStatus.SUCCESS_WITH_WARNINGS, WorkflowStatus.FAILED],
)
def test_a_finished_workflow_is_never_re_judged_by_the_timeout_check(client, terminal_status):
    """★ 已经到终态的工作流，哪怕心跳很旧，也**不许**被改判（验收标准②）。

    这条是变异测试①的靶子：把"先判终态再判超时"的顺序颠倒，这里必须变红。
    """
    stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
    finished = datetime.now(timezone.utc) - timedelta(seconds=3500)
    _register(status=terminal_status, step="done", heartbeat_at=stale, finished_at=finished)

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == terminal_status.value
    # 终态工作流的历史 error 不该被超时逻辑覆盖成 WORKFLOW_TIMEOUT
    assert data["error"] is None


def test_a_fresh_heartbeat_is_not_treated_as_a_timeout(client):
    """心跳新鲜就别乱判超时：正在正常跑的工作流必须保持原状态。"""
    _register(status=WorkflowStatus.EXECUTING, heartbeat_at=datetime.now(timezone.utc))

    data = client.get(f"/api/workflows/{WORKFLOW_ID}").json()["data"]

    assert data["status"] == "EXECUTING"
    assert data["error"] is None


def test_a_workflow_that_never_started_is_not_treated_as_a_timeout(client):
    """还没开始跑（没有心跳）时不判超时：那是 orchestrator 自己的超时逻辑管的事。"""
    _register(status=WorkflowStatus.PENDING, heartbeat_at=None)

    data = client.get(f"/api/workflows/{WORKFLOW_ID}").json()["data"]

    assert data["status"] == "PENDING"
    assert data["error"] is None


def test_status_interface_returns_404_for_an_unknown_workflow(client):
    """没登记过的 workflow_id → 404 NOT_FOUND（不是 ok:false 的 200）。"""
    resp = client.get("/api/workflows/" + "f" * 32)

    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_report_interface_returns_404_for_an_unknown_workflow(client):
    """接口 7 的第一条分支：工作流不存在 → 404。"""
    resp = client.get(f"/api/workflows/{'f' * 32}/report")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_report_interface_returns_400_when_the_report_is_not_ready(client):
    """接口 7 的第二条分支：工作流在、报告没生成 → 400「报告尚未生成」。

    400 而不是 404：资源是存在的，是**取报告的时机**不对。
    """
    _register(status=WorkflowStatus.EXECUTING)

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/report")

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == ErrorCode.BAD_REQUEST.value
    assert "报告尚未生成" in error["message"]
    # detail 要带上当前状态，用户才知道自己等的是哪一步
    assert "EXECUTING" in error["detail"]


def test_report_interface_returns_the_report_and_the_raw_results(client):
    """接口 7 的正常分支：报告 + 校验结果 + 原始工具结果 + llm_used。"""
    report = _make_report()
    result = _make_result()
    validation = ValidationResult(
        passed=True,
        checked_numbers=1,
        checked_at=datetime.now(timezone.utc),
    )
    _register(
        status=WorkflowStatus.SUCCESS,
        step="done",
        finished_at=datetime.now(timezone.utc),
        report=report,
        results=[result],
        validation=validation,
    )

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/report")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["report"]["title"] == "销售额按地区分析"
    assert data["report"]["llm_used"] is False
    assert data["validation"]["passed"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["task_id"] == "T1"
    # llm_used 与 report.llm_used 同源（§7.1 第 4 条），不许各存一份
    assert data["llm_used"] == data["report"]["llm_used"]


def test_report_interface_works_without_a_validation_result(client):
    """报告有、校验结果没有（例如校验阶段挂了）时也要能取到报告。

    `validation` 是可选字段：报告和校验是两步，前一步成功不代表后一步跑过。
    """
    _register(status=WorkflowStatus.SUCCESS_WITH_WARNINGS, step="done", report=_make_report())

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/report")

    assert resp.status_code == 200
    assert resp.json()["data"]["validation"] is None


@pytest.mark.parametrize("bad_filename", ILLEGAL_FILENAMES)
def test_chart_interface_rejects_an_illegal_filename(bad_filename):
    """接口 8 的第一道判断：文件名非法 → 400（**直接调函数**，绕开 URL 规范化）。

    直接调是关键：HTTP 客户端与 Web 框架会先处理 `..`、`//` 这些片段，
    走 HTTP 测出来的可能是框架的行为。这里要证明的是**这段代码自己会拒绝**。
    """
    _register()  # 工作流存在，所以 400 只能来自文件名校验

    with pytest.raises(AppError) as excinfo:
        get_chart(WORKFLOW_ID, bad_filename)

    assert excinfo.value.code is ErrorCode.BAD_REQUEST


def test_chart_interface_rejects_a_bad_workflow_id():
    """workflow_id 形态不对同样算"请求写错了" → 400，而不是拿它去拼路径。"""
    with pytest.raises(AppError) as excinfo:
        get_chart("not-a-workflow-id", CHART_FILENAME)

    assert excinfo.value.code is ErrorCode.BAD_REQUEST


def test_chart_interface_rejects_an_illegal_filename_over_http(client):
    """HTTP 这条路也要有一条：`abc.png` 不是 12 位 hex → 400。"""
    _register()

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/charts/abc.png")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == ErrorCode.BAD_REQUEST.value


def test_chart_interface_returns_404_when_the_file_is_missing(client):
    """文件名合法但磁盘上没有这个文件 → 404（请求合法，只是图还没生成）。"""
    _register()

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/charts/{CHART_FILENAME}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_chart_interface_returns_404_when_the_workflow_is_unknown(client):
    """文件名合法、工作流不存在 → 404（接口 8 也要认工作流）。"""
    resp = client.get(f"/api/workflows/{'f' * 32}/charts/{CHART_FILENAME}")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_chart_interface_serves_a_real_png(client):
    """接口 8 的正常分支：把 PNG 原样送出来，Content-Type 是 image/png。"""
    _register()
    charts_dir = config.get_settings().charts_path
    target = charts_dir / WORKFLOW_ID / CHART_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(FAKE_PNG)

    resp = client.get(f"/api/workflows/{WORKFLOW_ID}/charts/{CHART_FILENAME}")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == FAKE_PNG


def test_the_router_exposes_exactly_the_contract_paths():
    """路由路径必须与契约逐字一致（task-32 起多了两个历史接口），且 router 自己**不能再加 prefix**。

    为什么这条值得写：`/api` 这一段由 `app/main.py` 统一挂。如果这里写了
    `prefix="/workflows"`，最终路径会变成 `/api/workflows/workflows/{id}` ——
    前端拿到的是一个 404 的地址，而且**没有任何测试会因此变红**（除非写了这一条）。

    task-32 补记：`/workflows/history` 与 `/workflows/history/{workflow_id}` 必须声明在
    `/workflows/{workflow_id}` **之前**，否则 FastAPI 会把 `history` 当成一个 workflow_id
    去匹配（路由顺序的专项用例在 tests/test_history_api.py）。这条集合相等断言同时兜住
    “有人又悄悄加/删了一条路径”——集合相等而不是包含，是因为多一条少一条都是契约变更。
    """
    assert router.prefix == ""
    assert {route.path for route in router.routes} == {
        "/workflows/history",
        "/workflows/history/{workflow_id}",
        "/workflows/{workflow_id}",
        "/workflows/{workflow_id}/report",
        "/workflows/{workflow_id}/charts/{filename:path}",
    }
    # 挂到真实 app 上之后，前缀确实只有一层 /api
    assert {route.path for route in app.routes if route.path.startswith("/api/workflows")} == {
        "/api/workflows/history",
        "/api/workflows/history/{workflow_id}",
        "/api/workflows/{workflow_id}",
        "/api/workflows/{workflow_id}/report",
        "/api/workflows/{workflow_id}/charts/{filename:path}",
    }


def test_a_real_workflow_can_be_polled_through_http(client):
    """端到端：真上传 → 真受理 → 用接口 6 轮询。

    只断言"接口这一层"的性质（200、字段形状、终态），**不断言分析结果**：
    分析链路是别人负责的，它失败时状态会是 FAILED —— 那也是一条合法的终态，
    接口照样要能把它读出来（这正是"工作流失败也是 HTTP 200"的用处）。

    因为 `TestClient` 会等后台任务跑完（实测过），POST 返回时工作流通常已经结束了，
    这里不需要轮询循环。
    """
    with SAMPLE_CSV.open("rb") as handle:
        upload = client.post(
            "/api/datasets/upload",
            files={"file": ("sales.csv", handle, "text/csv")},
        )
    assert upload.status_code == 200
    dataset_id = upload.json()["data"]["dataset_id"]

    accepted = client.post(
        "/api/analysis",
        json={"dataset_id": dataset_id, "question": "按地区看销售额"},
    )
    assert accepted.status_code == 202
    workflow_id = accepted.json()["data"]["workflow_id"]

    resp = client.get(f"/api/workflows/{workflow_id}")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["workflow_id"] == workflow_id
    assert data["status"] in TERMINAL_STATUS_NAMES
    assert "report" not in data
    assert "results" not in data
    # has_report 与实际能不能取到报告必须一致：报告在就 true（接口 7 会给 200）
    report_resp = client.get(f"/api/workflows/{workflow_id}/report")
    assert data["has_report"] is (report_resp.status_code == 200)
