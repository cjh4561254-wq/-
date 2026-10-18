"""`POST /api/analysis` 的接口测试：受理、404、422、立刻返回、后台兜底。

写之前先记下三条实测教训（这个文件按它们写，不然很容易写出"假绿"）
----------------------------------------------------------------
1. `TestClient(app)` 必须写成 `with TestClient(app) as client:`。
   lifespan 只在**进入上下文**的时候跑，模块级 `TestClient(app)` 不会跑：
   上传目录建不出来，接口一落盘就 500。`tests/test_health.py` 早期那个"假绿"
   就是这么来的。
2. `registry` 是**进程内全局状态**，用例之间会互相污染。所以每个用例进出一律
   `registry.clear()`，另外保留一个 `teardown_function` 兜底（QA 提的 D-16：
   正序跑完残留 0 条，反序跑完残留 1 条 —— 最后一条用例登记的状态会活到会话结束）。
3. **`TestClient` 会等后台任务跑完**。本机实测：后台任务睡 1.2 秒，`client.post`
   就花了 1.204 秒。所以"用 `client.post` 的耗时证明接口没有等分析跑完"是**假的**
   —— 它测的是接口 + 整条分析链路。真正的证明放在
   `test_endpoint_does_not_wait_for_the_analysis`：直接调路由函数 + 打桩一个很慢的
   `run_workflow`，断言它在 1 秒内返回、并且后台任务**一次都没跑**。

另外两条纪律：
- 上传目录一律指到 `tmp_path`：接口会真的落盘，不能污染仓库里的 `data/uploads/`；
- LLM 三项配置在夹具里显式清空：接口测试不该受 `.env` 影响，更不该真的发出网络
  请求（规则兜底模式覆盖不到的路径，就是不该在单测里走的路径）。
"""

import re
import time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from app import config
from app.api.routes_analysis import start_analysis
from app.core import background as background_module
from app.core import registry
from app.core.auth import SessionUser
from app.core.background import _run_safely, submit
from app.main import app
from app.schemas import ErrorCode
from app.schemas_workflow import (
    TERMINAL_STATUSES,
    QuestionRequest,
    WorkflowState,
    WorkflowStatus,
)
from tests import auth_support

# 发请求的客户端（`client` 夹具）就是 `tests/auth_support.py` 造的 test-admin，
# 它在测试账号库里的 id 是 1（bootstrap 建的第一个账号）。直接调路由函数时要自己
# 把这个"当前用户"传进去 —— `Depends(require_user)` 只在走 HTTP 时才生效。
CURRENT_USER = SessionUser(id=1, username=auth_support.TEST_USERNAME, display_name="测试管理员")

# 示例数据集的真实路径。分析链路跑的就是它（2168 行 × 9 列）。
SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

# 用来直接构造 WorkflowState 的合法值：workflow_id 必须匹配 `^[0-9a-f]{32}$`。
SOME_WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
SOME_DATASET_ID = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """每个用例：上传目录隔离 + 清空进程内状态 + 清掉 LLM 配置。

    进去之前也清一遍（不只是出来清）：这样单跑这一个文件时不受别的测试文件残留
    影响，整个会话顺序调换也不会变红。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()
    registry.clear()
    yield
    registry.clear()
    config.reset_settings_cache()


def teardown_function() -> None:
    """用例结束后再兜一次底：最后一条用例登记的状态也不能活到会话结束（D-16）。"""
    registry.clear()


def _upload_sample(client: TestClient) -> str:
    """走真实上传接口拿一个 `dataset_id`（不 mock store：接口层要测的就是真实链路）。"""
    with SAMPLE_CSV.open("rb") as handle:
        response = client.post(
            "/api/datasets/upload",
            files={"file": ("sales.csv", handle, "text/csv")},
        )
    assert response.status_code == 200, response.text
    return response.json()["data"]["dataset_id"]


def _make_state() -> WorkflowState:
    """直接造一条状态，用来单测后台函数（绕开 HTTP，异常才抓得住）。"""
    return WorkflowState(
        workflow_id=SOME_WORKFLOW_ID,
        dataset_id=SOME_DATASET_ID,
        question="各地区销售额对比",
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# 受理成功：202 + 只回 4 个字段 + PENDING
# ---------------------------------------------------------------------------


def test_submit_returns_202_with_a_pending_workflow():
    """POST /api/analysis → 202，body 是 PENDING 的受理回执。"""
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        response = client.post(
            "/api/analysis",
            json={"dataset_id": dataset_id, "question": "各地区销售额对比"},
        )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["error"] is None

    data = body["data"]
    # workflow_id 是 32 位小写 hex（没有连字符）：前端拿它轮询，格式就是契约。
    assert re.fullmatch(r"[0-9a-f]{32}", data["workflow_id"])
    assert data["status"] == "PENDING"
    assert data["dataset_id"] == dataset_id

    # 只回 4 个字段：state 的完整 dump（plan/results/tool_calls…）不进响应体。
    assert set(data) == {"workflow_id", "dataset_id", "status", "created_at"}


# ---------------------------------------------------------------------------
# 两种"请求本身错了"：404 与 422
# ---------------------------------------------------------------------------


def test_unknown_dataset_id_gets_404():
    """没登记过的 dataset_id → 真实 404（不是 200 里塞个 ok:false）。"""
    with TestClient(app) as client:
        response = client.post(
            "/api/analysis",
            json={"dataset_id": SOME_DATASET_ID, "question": "各地区销售额对比"},
        )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("question", ["", "查"])
def test_question_too_short_gets_422(question: str):
    """问题为空或只有 1 个字 → 422（契约里是 min_length=2）。"""
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        response = client.post(
            "/api/analysis",
            json={"dataset_id": dataset_id, "question": question},
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize("max_steps", [0, 9])
def test_max_steps_out_of_range_gets_422(max_steps: int):
    """max_steps 越界 → 422（契约里是 ge=1, le=8）。"""
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        response = client.post(
            "/api/analysis",
            json={
                "dataset_id": dataset_id,
                "question": "各地区销售额对比",
                "max_steps": max_steps,
            },
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# 后台执行器：两条"绝不能让前端永远转圈"的路径
# ---------------------------------------------------------------------------


def test_run_safely_turns_a_crash_into_a_failed_status(monkeypatch):
    """`run_workflow` 抛普通异常 → 状态落成 FAILED，并且有收尾时间。"""
    def boom(state: WorkflowState) -> None:
        raise RuntimeError("分析炸了")

    monkeypatch.setattr(background_module, "run_workflow", boom)
    state = _make_state()

    _run_safely(state)  # 直接调：异常发生在后台线程里，用 HTTP 测抓不到

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.INTERNAL_ERROR
    assert "分析炸了" in (state.error.detail or "")
    assert state.started_at is not None
    assert state.finished_at is not None
    assert state.step == "done"


def test_run_safely_survives_a_base_exception(monkeypatch):
    """`KeyboardInterrupt`（`BaseException`，不是 `Exception`）也必须被收成 FAILED。

    这条是在钉 `except BaseException` 这个决定：只写 `except Exception` 的话，
    异常会穿透、状态永远停在 PROFILING，前端就无限转圈。
    """
    def interrupted(state: WorkflowState) -> None:
        raise KeyboardInterrupt("用户按了 Ctrl+C")

    monkeypatch.setattr(background_module, "run_workflow", interrupted)
    state = _make_state()

    _run_safely(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert "Ctrl+C" in (state.error.detail or "")
    assert state.step == "done"
    assert state.finished_at is not None


def test_submit_registers_exactly_one_background_task():
    """`submit` 只登记一个后台任务，而且登记的就是 `_run_safely`。"""
    state = _make_state()
    tasks = BackgroundTasks()

    submit(state, tasks)

    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func is _run_safely
    assert tasks.tasks[0].args == (state,)


# ---------------------------------------------------------------------------
# "受理完就返回"的决定性证明（见文件 docstring 第 3 条）
# ---------------------------------------------------------------------------


def test_endpoint_does_not_wait_for_the_analysis(monkeypatch):
    """接口自己不等分析：直接调函数 + 打桩一个睡 2 秒的 `run_workflow`。

    断言三件事：
        ① 调用本身在 1 秒内返回（真的没等）；
        ② 那个很慢的后台函数**一次都没被执行**（`BackgroundTasks` 只是登记）；
        ③ 返回的是 PENDING，且状态已经登记进 registry（前端马上轮询得到）。

    这里直接调路由函数（不走 HTTP），所以 `user` 依赖不会生效，必须自己传
    `CURRENT_USER` —— 不传的话 `require_owned_dataset(store.get(...), user.id)`
    拿到的是 `Depends` 对象，报 `AttributeError: 'Depends' object has no attribute 'id'`。
    归属判定那一关仍然是真跑的：`dataset_id` 是上面用同一个用户上传的。
    """
    ran: list[str] = []
    slept = 2.0

    def slow_workflow(state: WorkflowState) -> None:
        ran.append(state.workflow_id)
        time.sleep(slept)

    monkeypatch.setattr(background_module, "run_workflow", slow_workflow)

    with TestClient(app) as client:
        dataset_id = _upload_sample(client)

    tasks = BackgroundTasks()
    started = time.perf_counter()
    accepted = start_analysis(
        QuestionRequest(dataset_id=dataset_id, question="各地区销售额对比"),
        tasks,
        CURRENT_USER,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"接口自己花了 {elapsed:.3f} 秒，说明它在等分析跑完"
    assert ran == [], "后台任务在接口返回之前就被执行了，那样 202 就是假的"
    assert len(tasks.tasks) == 1
    assert accepted.data.status == "PENDING"
    assert registry.get(accepted.data.workflow_id) is not None


def test_http_round_trip_is_under_one_second():
    """走完整 HTTP 的耗时上限（**包含**后台分析，见文件 docstring 第 3 条）。

    这条不是"没在等"的证明，而是"整条受理 + 分析链路在这个数据集上足够快"的
    护栏：真跑一次，超过 1 秒就说明有东西变慢了（比如某个工具退化成慢查询）。
    """
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        started = time.perf_counter()
        response = client.post(
            "/api/analysis",
            json={"dataset_id": dataset_id, "question": "各地区销售额对比"},
        )
        elapsed = time.perf_counter() - started

    assert response.status_code == 202, response.text
    assert elapsed < 1.0, f"受理 + 分析跑了 {elapsed:.3f} 秒，超过 1 秒"


# ---------------------------------------------------------------------------
# 轮询能看到状态往前走
# ---------------------------------------------------------------------------


def test_polling_can_see_the_workflow_progress():
    """提交之后，registry 里那条工作流会从"排队"走到终态。

    说明：`TestClient` 会等后台任务跑完，所以这里循环通常第一轮就跳出；循环本身
    是为"以后把后台换成真异步"准备的 —— 到那时它会真的等状态变化。
    """
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        response = client.post(
            "/api/analysis",
            json={"dataset_id": dataset_id, "question": "各地区销售额对比"},
        )

    assert response.status_code == 202, response.text
    workflow_id = response.json()["data"]["workflow_id"]

    deadline = time.perf_counter() + 10.0
    state: WorkflowState | None = None
    while time.perf_counter() < deadline:
        state = registry.get(workflow_id)
        if state is not None and (
            state.status in TERMINAL_STATUSES or state.step != "profiling"
        ):
            break
        time.sleep(0.05)

    assert state is not None, "提交之后 registry 里应该有这条工作流"
    assert state.started_at is not None, "后台线程真的跑过（开始时间被记下了）"
    assert state.finished_at is not None, "_run_safely 的 finally 一定盖上结束时间"
    assert state.step == "done"
    # 这里原来还有一行 `assert state.status in TERMINAL_STATUSES or state.step == "done"`，
    # 它是同义反复：上一行已经断言 `step == "done"`，`or` 的右半边必然为真，
    # 整个断言恒成立，status 是什么都过。看着像在守"走到终态了"，实际一个字符都没守。
    # D-22 第 2 条：期望值两边相同的探针 = 没有探针。改成直接钉 status。
    assert state.status in TERMINAL_STATUSES, "后台真的跑完了，状态得是终态"


def test_the_route_module_has_no_prefix_of_its_own():
    """`/api/analysis` 不能再被拼一层：router 自己不能带 prefix。

    架构文档 §8.2 / §4.14.3 的装饰器原文是 `@router.post("/analysis", ...)`，
    `/api` 由 `app/main.py` 统一挂。要是谁给 router 加了 `prefix="/analysis"`，
    路径会变成 `/api/analysis/analysis` —— 这条断言就是拦它的。
    """
    from app.api.routes_analysis import router

    assert router.prefix == ""
    paths = {route.path for route in router.routes}
    assert paths == {"/analysis"}


def test_response_never_leaks_internal_state_fields():
    """响应体里不许出现内部字段名（results / plan / tool_calls / step_index）。"""
    with TestClient(app) as client:
        dataset_id = _upload_sample(client)
        response = client.post(
            "/api/analysis",
            json={"dataset_id": dataset_id, "question": "各地区销售额对比"},
        )

    raw: dict[str, Any] = response.json()
    text = str(raw)
    for hidden in ("tool_calls", "step_index", "heartbeat_at", "plan", "results"):
        assert hidden not in text, f"响应体里漏了内部字段 {hidden}"
