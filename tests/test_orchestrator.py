"""`app/agents/orchestrator.py` 与 `app/agents/validation.py` 的测试。

这个文件在测什么
----------------
`run_workflow()` 是全项目唯一"把六步串起来"的地方，也是唯一有权改
`state.status` / `state.step` / `state.retry_count` 的地方。它写错的表现通常不是
崩溃，而是**状态卡住**：前端一直转圈、`status` 永远停在 `EXECUTING`、
`heartbeat_at` 不再更新。所以这里的断言几乎全部落在状态字段上。

四条主线：

1. **真跑一遍**：用真实的 `data/samples/sales.csv` + 规则兜底计划，
   从 `PENDING` 一路走到 `SUCCESS`，并检查报告里真的引用了工具算出来的数字。
2. **失败不致命**：单个任务失败、报告生成失败，都不该让整条工作流崩掉
   （架构文档 §5.4：单任务失败不中断工作流）。
3. **两个上限真的会拦住**：`tool_call_limit` 与 `max_workflow_seconds`。
   这两条如果只是"写在配置里但没人读"，线上表现就是工作流跑到天荒地老。
4. **不越界**：不出图、不直接调大模型、不 import Web 框架 —— 这三件事
   由架构文档 §3.3 明确禁止，用 AST 扫源码来钉。

为什么不污染全局 `store`
------------------------
`app/data/store.py` 里的 `store` 是**进程内单例**，而且没有 `unregister()`。
往它里面 `register()` 一次，这份数据就永远留在进程里了 —— 于是用例之间会通过
这个单例互相看见。所以下面每个用例都把 `orchestrator` 模块里的 `store` 名字
换成一个**独立的 `DatasetStore()` 实例**（monkeypatch 只改模块属性，不改全局单例）。
这比"注册完记得清理"可靠：没有需要记得的事。
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app import config
from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import run_workflow
from app.agents.validation import build_report
from app.data.loader import load_dataframe
from app.data.store import DatasetStore
from app.schemas import ApiError, ErrorCode
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    AnalysisTask,
    FinalReport,
    TaskPlan,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

# 两个问题各有用处：
# - 单任务问题，用来验证"最顺利的那条路"（状态、报告、引用都对）
# - 双任务问题（"缺失"+"各…按…"同时命中），用来验证多任务顺序与 tool_call_limit
ONE_TASK_QUESTION = "分析不同地区的销售额"
TWO_TASK_QUESTION = "检查数据集中的缺失值，并分析不同地区的销售额"

# 模板报告 summary 的首段，**逐字写死在测试里**（不从实现里 import 常量）。
# 理由见 test_report_first_paragraph_states_no_model_was_used 的 docstring：
# 拿常量和它自己比是永远为真的断言，抓不住"有人改了这个措辞"。
TEMPLATE_NOTICE = "本次未使用大模型，以下结论为工具结果的直接汇总"


@pytest.fixture(scope="module")
def sales_df():
    """真实的示例数据。整个模块共用一份，读盘只读一次。

    `load_dataframe` 返回 `(df, 编码)`，这里只关心 df —— 编码那条链路由
    `tests/test_loader.py` 负责。
    """
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture
def basket(monkeypatch, sales_df):
    """一个**独立**的 DatasetStore，并且换掉 orchestrator 眼里的那个 `store`。

    返回 `(store, dataset_id)`。用独立的实例而不是全局单例，理由见模块 docstring。
    """
    fake_store = DatasetStore()
    dataset_id = "0123456789abcdef0123456789abcdef"
    fake_store.register(dataset_id, sales_df, "sales.csv", "csv", 1024, "sha-test")
    monkeypatch.setattr(orchestrator_module, "store", fake_store)
    return fake_store, dataset_id


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """每个用例一套显式配置，结束后清缓存。

    `get_settings()` 带 `lru_cache`，不清的话下一个用例读到的是上一个用例的环境
    （本项目真踩过这个坑）。这里显式钉住两个上限，**不依赖 `.env` 的值** ——
    否则用户把 `TOOL_CALL_LIMIT` 改成 50，下面那两条"真的会拦住"的断言就失去意义了。
    """
    monkeypatch.setenv("MAX_WORKFLOW_SECONDS", "180")
    monkeypatch.setenv("TOOL_CALL_LIMIT", "20")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _make_state(dataset_id: str, question: str = ONE_TASK_QUESTION, **overrides: Any) -> WorkflowState:
    """造一个"已经被后台外壳接手"的状态。

    `started_at` 必须自己写：真实的调用链里它是 `app/core/background.py` 的
    `_run_safely()` 写的，不是 `run_workflow()` 写的。测试里不写的话，
    `_timed_out()` 会退回去看 `created_at`，行为虽然一样，但少了"后台真的启动过"这一环。
    """
    now = datetime.now(timezone.utc)
    fields: dict[str, Any] = {
        "workflow_id": "a" * 32,
        "dataset_id": dataset_id,
        "question": question,
        "created_at": now,
        "started_at": now,
    }
    fields.update(overrides)
    return WorkflowState(**fields)


# ---------------------------------------------------------------------------
# 1. 真实数据端到端
# ---------------------------------------------------------------------------


def test_workflow_runs_end_to_end_on_real_data(basket):
    """真实的 sales.csv + 规则兜底计划 → 一路走到 `SUCCESS`。

    这条是整个 D4 的"冒烟测试"：它一旦红，说明六步里至少有一环断了。
    断言刻意只钉**结构性事实**（状态、来源、有没有报告），不钉具体数字 ——
    具体数字由 `tests/test_planner_rules.py` 与 `tests/test_tools.py` 负责，
    钉在这里会让"示例数据换个行数"变成三处一起红。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)

    result = run_workflow(state)

    assert result is None  # 契约：原地改 state，没有返回值
    assert state.status is WorkflowStatus.SUCCESS
    assert state.error is None
    assert state.step == "done"
    assert state.step_index == 6  # 六步全部走完
    assert state.plan is not None
    assert state.plan.source == "rule_fallback"  # 没配大模型 → 必然是规则兜底
    assert state.has_report is True
    assert state.report is not None
    assert state.report.llm_used is False


def test_report_sections_cite_the_tasks_that_produced_them(basket):
    """报告的每一节都必须能指回"这个数字是哪个任务算的"。

    没有 `cites` 的报告是没法核对的：读者看到"华南销售额 2487 万"，
    却不知道它出自哪个工具、哪个任务。这正是本项目"数字必须可追溯"的底线。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    report = state.report
    assert report is not None
    assert report.sections, "报告至少要有一节"
    for section in report.sections:
        assert section.cites, f"这一节没有 cites：{section.heading}"
        assert section.text

    # 引用的 task_id 必须真的在结果里出现过 —— 不是编出来的编号
    known = {result.task_id for result in state.results}
    assert set(report.citations) <= known


def test_report_first_paragraph_states_no_model_was_used(basket):
    """模板报告的 summary 首段必须逐字写清"这次没用到模型"。

    这一行不是装饰：用户看到一份排版整齐的报告，很容易默认"这是 AI 写的"，
    于是对里面的数字给出不该有的信任。模板路径必须自己把这件事说破。

    这句话为什么**写成字面量**而不是 `startswith(TEMPLATE_NOTICE)`：
    拿常量和它自己比，是一句永远为真的断言 —— 有人把常量改成别的措辞
    （比如删掉"未使用大模型"这半句），断言两边一起变，测试照样绿。
    这条测试要钉的恰恰是"用户看到的字必须长这样"，所以字必须写死在测试里。
    这是变异测试抓出来的：改坏 `TEMPLATE_NOTICE` 时它当时没有变红。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.report is not None
    assert state.report.summary.startswith(TEMPLATE_NOTICE)


def test_report_table_copies_tool_rows_without_recomputing(basket):
    """报告里的表格是 `ToolResult.data["rows"]` 的**原样副本**，不是重新算的。

    "不许重算"是架构文档 §3.3 的硬规矩：报告层只要开始自己聚合，就会出现
    "报告里的数字和工具返回的数字对不上"这种最难查的 bug。所以这里逐字段比对。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    report = state.report
    assert report is not None
    table_sections = [section for section in report.sections if section.table]
    assert table_sections, "分组汇总的结果必须带一张表"

    result = state.results[0]
    expected_rows = result.data["rows"]
    assert table_sections[0].table == expected_rows


def test_report_table_is_capped_at_twenty_rows(basket, monkeypatch):
    """表格最多 20 行 —— 上限生效，且截断发生在**复制之后**（顺序不能变）。"""
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    result = state.results[0]
    monkeypatch.setitem(result.data, "rows", [{"i": index} for index in range(50)])

    report = build_report(state)
    section = next(item for item in report.sections if item.table)
    assert len(section.table) == 20
    assert section.table == [{"i": index} for index in range(20)]


def test_workflow_renders_charts_on_the_rule_path(basket):
    """D5：规则路径（未配大模型）也必须出图，`state.charts` 非空且图真在磁盘上。

    这条本来叫 `test_workflow_keeps_charts_empty_in_d4`，断言 `state.charts == []`。
    D4 的名字就写着 "in_d4" —— 它的断言内容是**D4 的占位事实**，不是产品要求；
    D5 让 planner 给分组汇总配上图之后，它必然红。**这类红是预期取代，不是回归**，
    所以把断言反过来钉：D5 的规则路径必须出图，而且每张图都要能追到盘上的文件。

    「空着比伪造好」这条原则没有作废，只是换了位置 —— 现在由
    `tests/test_charts.py` 负责"失败时不登记半个对象"。

    三条断言各有分工：
    ① `charts` 非空 —— 证明 charting 阶段真的跑了（不是 D4 那个只打日志的占位）；
    ② PNG 真在磁盘上 —— 证明不是"内存里有条记录、文件没写"（前端会拿到死链）；
    ③ `url` 能拼回接口 8 的真实路径 —— 前端拿到 `chart.url` 就直接塞 `<img src>`，
       拼错的表现是图片裂开，而后端一切正常，最难查的一类。

    注意这条测试会往**真实的** `data/charts/` 里写 PNG（它没有把 `CHARTS_DIR`
    指到 `tmp_path`，因为要验的就是"按配置里的路径落盘"这件事）。
    目录由 `.gitignore` 的 `data/charts/*` 兜住，不会进版本库。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.charts, "规则路径也必须出图（D5 验收④）"
    for chart in state.charts:
        assert chart.point_count > 0, "图上至少要有一个数据点"

        path = config.get_settings().charts_path / state.workflow_id / chart.filename
        assert path.is_file(), f"图没有落盘：{path}"
        assert path.stat().st_size > 0, "PNG 是 0 字节，等于没有图"

        expected_url = f"/api/workflows/{state.workflow_id}/charts/{chart.filename}"
        assert chart.url == expected_url
        assert chart.chart_id in chart.url
        assert chart.url.endswith(".png")


def test_heartbeat_is_refreshed_during_the_run(basket):
    """跑完之后 `heartbeat_at` 必须是新的、且不早于开始时间。

    前端靠这个字段判断"后台是不是还活着"。它不更新的话，一个正常跑完的工作流
    在界面上看起来和卡死没有区别。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    assert state.heartbeat_at is None

    run_workflow(state)

    assert state.heartbeat_at is not None
    assert state.heartbeat_at >= state.started_at
    assert state.finished_at is not None
    assert state.finished_at >= state.heartbeat_at


def test_multi_task_plan_runs_every_task_in_order(basket):
    """同时命中两条规则时，两个任务都要跑，且 `order` 与执行顺序一致。"""
    _, dataset_id = basket
    state = _make_state(dataset_id, TWO_TASK_QUESTION)
    run_workflow(state)

    assert state.plan is not None
    assert len(state.plan.tasks) >= 2
    assert [task.task_id for task in state.plan.tasks] == [f"T{i}" for i in range(1, len(state.plan.tasks) + 1)]
    assert all(task.status is TaskStatus.SUCCESS for task in state.plan.tasks)
    assert len(state.results) == len(state.plan.tasks)


# ---------------------------------------------------------------------------
# 2. 失败不致命
# ---------------------------------------------------------------------------


def test_missing_dataset_fails_with_not_found(monkeypatch):
    """数据集不在内存里 → `FAILED` + `NOT_FOUND`，且**不去调工具**。

    服务重启后数据集就没了（§8.2.3：状态只在进程内存里）。这时给用户的
    必须是一句"需要重新上传"，而不是一堆工具抛出来的 KeyError。
    """
    empty_store = DatasetStore()
    monkeypatch.setattr(orchestrator_module, "store", empty_store)

    state = _make_state("f" * 32)
    run_workflow(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.NOT_FOUND
    assert "重新上传" in (state.error.detail or "")
    assert state.results == []
    assert state.report is None


def test_one_failed_task_does_not_abort_the_workflow(basket, monkeypatch):
    """一个任务失败，其余任务照跑，最终是 `SUCCESS_WITH_WARNINGS` 而不是 `FAILED`。

    架构文档 §5.4 第 1 条。这条如果不成立，"问一个稍微偏一点的问题"
    就会让整份报告消失 —— 而用户真正想要的是"能算的都算出来，算不了的说明原因"。
    """
    _, dataset_id = basket

    def fake_plan(question: str, profile: Any, max_steps: int = 6) -> TaskPlan:
        return TaskPlan(
            plan_id="deadbeef",
            question=question,
            dataset_id=profile.dataset_id,
            tasks=[
                AnalysisTask(
                    task_id="T1",
                    order=1,
                    kind="missing",
                    tool_name="missing_values",
                    # 引用一个不存在的列 → 校验第 3 关（COLUMN_NOT_FOUND）必然拒绝
                    params={"columns": ["不存在的列"]},
                    description="检查缺失值",
                ),
                AnalysisTask(
                    task_id="T2",
                    order=2,
                    kind="aggregate",
                    tool_name="group_aggregate",
                    params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
                    description="按地区汇总销售额",
                ),
            ],
            source="rule_fallback",
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(orchestrator_module, "plan_tasks", fake_plan)

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.plan is not None
    assert state.plan.tasks[0].status is TaskStatus.FAILED
    assert state.plan.tasks[0].error
    assert state.plan.tasks[1].status is TaskStatus.SUCCESS
    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert state.report is not None
    # 失败的那一节不能出现在报告里，成功的那节要在
    assert state.report.citations == ["T2"]


def test_dependent_task_is_skipped_when_its_dependency_fails(basket, monkeypatch):
    """`depends_on` 里的任务失败 → 下游任务标 `SKIPPED`，且**不去执行**。

    直接跑一个"依赖已经坏掉"的任务，结果通常是二次报错，把真正的原因
    （上游失败）埋掉。所以这里既断言状态，也断言工具调用次数没增加。
    """
    _, dataset_id = basket

    def fake_plan(question: str, profile: Any, max_steps: int = 6) -> TaskPlan:
        return TaskPlan(
            plan_id="deadbeef",
            question=question,
            dataset_id=profile.dataset_id,
            tasks=[
                AnalysisTask(
                    task_id="T1",
                    order=1,
                    kind="missing",
                    tool_name="missing_values",
                    params={"columns": ["不存在的列"]},
                    description="检查缺失值",
                ),
                AnalysisTask(
                    task_id="T2",
                    order=2,
                    kind="aggregate",
                    tool_name="group_aggregate",
                    params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
                    description="按地区汇总销售额",
                    depends_on=["T1"],
                ),
            ],
            source="rule_fallback",
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(orchestrator_module, "plan_tasks", fake_plan)

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.plan is not None
    assert state.plan.tasks[1].status is TaskStatus.SKIPPED
    assert "依赖" in (state.plan.tasks[1].error or "")
    assert state.tool_call_count == 1  # 只跑了 T1
    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS


def test_report_failure_does_not_fail_the_workflow(basket, monkeypatch):
    """报告生成炸了 → 工作流仍是"完成但有问题"，不是 `FAILED`。

    数字已经算出来了，报告只是排版。让排版问题把整条工作流判死，
    等于把已经拿到的结果扔掉。
    """
    _, dataset_id = basket

    def boom(state: WorkflowState) -> FinalReport:
        raise RuntimeError("报告炸了")

    monkeypatch.setattr(orchestrator_module, "build_report", boom)

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert state.has_report is False
    assert state.report is None
    assert state.error is not None
    assert state.error.code is ErrorCode.INTERNAL_ERROR
    # 已经算出来的结果不能被丢掉
    assert state.results


def test_a_state_carrying_an_error_is_never_reported_as_clean_success(basket):
    """报告已经生成、但状态里同时挂着一个 error 时，最终状态必须是"完成但有问题"。

    这条补的是上一条够不到的分支：报告生成失败时 `state.report` 是 `None`，
    光靠"报告不存在"这一半条件就已经返回了，所以 `_final_status()` 里
    `or state.error is not None` 这半句在那条路径上**永远不会被求值到**。
    变异测试把后半句删掉时测试全绿，就是因为它没被覆盖。

    为什么这半句必须留着：`state.error` 会被别的地方写上（`app/core/background.py`
    的 `_run_safely()` 在兜住未预期异常时会写）。如果判状态时只看报告在不在，
    就会出现"响应里 `status=SUCCESS`，同一个响应里 `error` 却不是 null"这种
    自相矛盾的结果 —— 前端只能二选一显示，无论选哪个都是错的。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)

    # 先造出一份"真的报告"，再往状态上挂一个错误，模拟收尾阶段才暴露的问题
    state.report = build_report(state)
    state.error = ApiError(code=ErrorCode.INTERNAL_ERROR, message="收尾时记下的错误")

    assert orchestrator_module._final_status(state) is WorkflowStatus.SUCCESS_WITH_WARNINGS


def test_planner_failure_fails_the_workflow_cleanly(basket, monkeypatch):
    """连计划都生不出来 → `FAILED`，而且错误信息是中文、不是裸异常字符串。"""
    _, dataset_id = basket

    def boom(question: str, profile: Any, max_steps: int = 6) -> TaskPlan:
        raise RuntimeError("规划炸了")

    monkeypatch.setattr(orchestrator_module, "plan_tasks", boom)

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.INTERNAL_ERROR
    assert "制定分析计划时出错" in state.error.message
    assert state.results == []


# ---------------------------------------------------------------------------
# 2b. 重试规则：只有 BAD_RESULT 才重试，且只重试一次
# ---------------------------------------------------------------------------
#
# 这一节是**变异测试逼出来的**。最初的用例集里没有一条能区分"重试了"和
# "没重试"：把 `_execute_one()` 里整段重试代码删掉，23 条用例照样全绿。
# 也就是说 §5.3 那张重试表虽然写在架构文档里，却没有任何断言在看着它。
#
# 重试这件事本身不难写错，难的是**写宽**：把 COLUMN_NOT_FOUND 也拿去重试，
# 表现是"每次问一个不存在的列，都要多等一次工具调用"——不报错、不变红，
# 只是白白慢一倍。所以下面既钉"该重试的真的重试了"，也钉"不该重试的一次都不多跑"。


def _fake_result(
    task: AnalysisTask,
    *,
    ok: bool,
    error_code: str | None = None,
    message: str = "",
) -> ToolResult:
    """造一个工具结果。

    直接 monkeypatch `orchestrator` 眼里的 `run_task`，而不是去注册一个真会抛
    异常的工具：这里要验的是**编排层的重试决策**，不是工具层怎么失败。
    把工具层换掉，就能精确控制"第几次调用返回什么"。
    """
    return ToolResult(
        task_id=task.task_id,
        tool_name=task.tool_name,
        ok=ok,
        data={"rows": [{"列": "值"}], "columns": ["列"], "row_count": 1} if ok else {},
        columns=["列"] if ok else [],
        row_count=1 if ok else 0,
        elapsed_ms=1,
        error_code=error_code,
        error_message=message or None,
        created_at=datetime.now(timezone.utc),
    )


def _single_task_plan(profile: Any) -> TaskPlan:
    """一个只有 1 个任务的计划。任务少，才能把"调用了几次"数清楚。"""
    return TaskPlan(
        plan_id="cafebabe",
        question="测试用问题",
        dataset_id=profile.dataset_id,
        tasks=[
            AnalysisTask(
                task_id="T1",
                order=1,
                kind="missing",
                tool_name="missing_values",
                params={},
                description="测试任务",
            )
        ],
        source="rule_fallback",
        created_at=datetime.now(timezone.utc),
    )


def test_bad_result_is_retried_once_and_both_attempts_are_kept(basket, monkeypatch):
    """第一次 `BAD_RESULT`、第二次成功 → 任务算成功，且**两次结果都留在 `results` 里**。

    为什么要留两次：排查线上问题时，"第一次报了什么错"往往比"最终成功了"更有信息量
    —— 它告诉你哪个工具在什么输入下不稳。只留最后一次的话，重试就把证据擦掉了。
    """
    _, dataset_id = basket
    seen_attempts: list[int] = []

    def fake_run_task(task: AnalysisTask, df: Any) -> ToolResult:
        seen_attempts.append(task.attempt)
        if len(seen_attempts) == 1:
            return _fake_result(task, ok=False, error_code="BAD_RESULT", message="第一次炸了")
        return _fake_result(task, ok=True)

    monkeypatch.setattr(orchestrator_module, "run_task", fake_run_task)
    monkeypatch.setattr(orchestrator_module, "plan_tasks", lambda q, p, max_steps=6: _single_task_plan(p))

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.plan is not None
    task = state.plan.tasks[0]
    assert task.status is TaskStatus.SUCCESS
    assert task.error is None
    assert task.attempt == 1
    assert state.retry_count == 1
    assert len(state.results) == 2
    assert state.results[0].ok is False
    assert state.results[1].ok is True
    # 重试也是一次工具调用，必须计入上限（否则上限会被重试绕过去）
    assert state.tool_call_count == 2
    # 第一次 attempt=0，重试前才自增，所以第二次看到的是 1
    assert seen_attempts == [0, 1]

    # 任务最终成功了，但整条工作流**不是干净的 SUCCESS** —— 它中途出过问题。
    # 这一条看着像是"太严"，其实是唯一能区分"一次就成"和"重试才成"的地方：
    # 只看任务状态的话两者都是 SUCCESS，用户就无从知道这次分析其实抖了一下。
    # 也正是这一条抓住了 `_final_status()` 里"有失败结果也算有话说"那半句 ——
    # 变异测试证明：删掉它，只有这条用例会变红（其余路径被"任务状态"那一关挡住了）。
    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert state.report is not None


def test_bad_result_twice_ends_as_failed_after_one_retry(basket, monkeypatch):
    """两次都是 `BAD_RESULT` → 任务 `FAILED`，但**只重试一次**，不无限重试。

    `attempt` 的上限是 `AnalysisTask` 契约里写死的 `le=2`；这里断言它停在 1，
    是因为 `_MAX_TASK_RETRIES = 1` —— 第一次失败后重试一次，一共两次。
    """
    _, dataset_id = basket
    seen_attempts: list[int] = []

    def fake_run_task(task: AnalysisTask, df: Any) -> ToolResult:
        seen_attempts.append(task.attempt)
        return _fake_result(task, ok=False, error_code="BAD_RESULT", message="每次都炸")

    monkeypatch.setattr(orchestrator_module, "run_task", fake_run_task)
    monkeypatch.setattr(orchestrator_module, "plan_tasks", lambda q, p, max_steps=6: _single_task_plan(p))

    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.plan is not None
    task = state.plan.tasks[0]
    assert task.status is TaskStatus.FAILED
    assert task.attempt == 1
    assert task.error == "每次都炸"
    assert state.retry_count == 1
    assert len(state.results) == 2
    assert seen_attempts == [0, 1]
    # 有任务失败但报告照出 → "完成但有问题"，不是 FAILED（§5.4 第 1 条）
    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert state.report is not None


@pytest.mark.parametrize(
    "error_code",
    ["TOOL_NOT_FOUND", "BAD_PARAMS", "COLUMN_NOT_FOUND", "BAD_OPERATION"],
)
def test_validation_errors_are_never_retried(basket, monkeypatch, error_code):
    """校验类错误一次都不重试（§5.3 表里前两行：不重试，0 次）。

    参数是模型给的。同一个参数再喂一遍，只会得到同一个拒绝 —— 重试除了拖慢
    响应没有任何作用。这条钉住"重试只对 BAD_RESULT 生效"。
    """
    _, dataset_id = basket
    call_count = 0

    def fake_run_task(task: AnalysisTask, df: Any) -> ToolResult:
        nonlocal call_count
        call_count += 1
        return _fake_result(task, ok=False, error_code=error_code, message=f"被拒绝：{error_code}")

    monkeypatch.setattr(orchestrator_module, "run_task", fake_run_task)
    monkeypatch.setattr(orchestrator_module, "plan_tasks", lambda q, p, max_steps=6: _single_task_plan(p))

    state = _make_state(dataset_id)
    run_workflow(state)

    assert call_count == 1, f"{error_code} 不该重试，但工具被调用了 {call_count} 次"
    assert state.plan is not None
    task = state.plan.tasks[0]
    assert task.status is TaskStatus.FAILED
    assert task.attempt == 0
    assert state.retry_count == 0
    assert len(state.results) == 1
    assert state.tool_call_count == 1


def test_retry_budget_exhaustion_skips_the_retry(basket, monkeypatch):
    """`state.retry_count` 已经到顶（6）→ 即使错误是 `BAD_RESULT` 也不重试。

    这是第二个上限：单个任务的重试次数有 `le=2`，整条工作流还有 6 次的总预算。
    没有这一条的话，"重试"在一条任务很多的计划上会累积成很长的响应时间。
    """
    _, dataset_id = basket
    call_count = 0

    def fake_run_task(task: AnalysisTask, df: Any) -> ToolResult:
        nonlocal call_count
        call_count += 1
        return _fake_result(task, ok=False, error_code="BAD_RESULT", message="炸了")

    monkeypatch.setattr(orchestrator_module, "run_task", fake_run_task)
    monkeypatch.setattr(orchestrator_module, "plan_tasks", lambda q, p, max_steps=6: _single_task_plan(p))

    state = _make_state(dataset_id)
    state.retry_count = 6  # 预算已用尽
    run_workflow(state)

    assert call_count == 1
    assert state.retry_count == 6
    assert state.plan is not None
    assert state.plan.tasks[0].status is TaskStatus.FAILED


# ---------------------------------------------------------------------------
# 3. 两个上限真的会拦住
# ---------------------------------------------------------------------------

def test_tool_call_limit_stops_the_workflow(basket, monkeypatch):
    """`tool_call_limit` 调到 1、计划里有 2 个任务 → 只跑 1 个就中止。

    这个上限存在的意义是"防止某个环节退化成无限循环"。所以断言的重点是
    **工具调用次数真的没超**，而不是只检查错误码 —— 只检查错误码的话，
    一个"先跑完 20 个任务再报上限"的实现也能通过。
    """
    _, dataset_id = basket
    monkeypatch.setenv("TOOL_CALL_LIMIT", "1")
    config.reset_settings_cache()

    state = _make_state(dataset_id, TWO_TASK_QUESTION)
    run_workflow(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.INTERNAL_ERROR
    assert "上限" in state.error.message
    assert state.tool_call_count == 1
    assert len(state.results) == 1


def test_workflow_timeout_is_reported_as_workflow_timeout(basket, monkeypatch):
    """超过 `max_workflow_seconds` → `FAILED` + `WORKFLOW_TIMEOUT`。

    `WORKFLOW_TIMEOUT` 在 HTTP 层是 **200**（§7.0）：超时不是"请求失败"，
    而是"分析没能在时限内完成"，客户端拿到的应该是一份说明而不是一个 5xx。
    """
    _, dataset_id = basket
    monkeypatch.setenv("MAX_WORKFLOW_SECONDS", "1")
    config.reset_settings_cache()

    state = _make_state(dataset_id)
    state.started_at = datetime.now(timezone.utc) - timedelta(seconds=30)

    run_workflow(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.WORKFLOW_TIMEOUT
    assert state.results == []


def test_naive_started_at_does_not_crash_the_timeout_check(basket):
    """`started_at` 是"不带时区"的时间时也不能崩。

    真实调用链里时间一律是 UTC 带时区的，但状态是可以被序列化、反序列化、
    甚至被测试直接构造的。朴素时间减带时区时间会抛 `TypeError`，
    那会变成一个 500 —— 而它本来的意思只是"超时了"。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    state.started_at = datetime(2000, 1, 1)  # 朴素时间，且远在过去

    run_workflow(state)

    assert state.status is WorkflowStatus.FAILED
    assert state.error is not None
    assert state.error.code is ErrorCode.WORKFLOW_TIMEOUT


# ---------------------------------------------------------------------------
# 4. build_report 自身的健壮性
# ---------------------------------------------------------------------------


def test_build_report_never_raises_on_an_empty_state(basket):
    """一个什么都没跑的状态也要能生成报告（只有一段说明），不能抛异常。

    `FinalReport.sections` 有 `min_length=1`，所以"没有结果"这件事
    必须变成一段话，而不是一个空列表 —— 空列表会直接被 Pydantic 拒绝。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)

    report = build_report(state)

    assert isinstance(report, FinalReport)
    assert len(report.sections) >= 1
    assert report.summary.startswith(TEMPLATE_NOTICE)
    assert report.citations == []


def test_build_report_works_without_a_profile(basket):
    """没有画像时也要能出报告 —— `data_quality_notes` 退化成空列表。"""
    _, dataset_id = basket
    state = _make_state(dataset_id)
    state.profile = None

    report = build_report(state)

    assert report.data_quality_notes == []


def test_build_report_reads_missing_value_results(basket):
    """`missing_values` 的结果要被翻译成中文句子，而不是把 dict 打印出来。

    用户看的是报告，不是 JSON。工具返回 `null_ratio: 0.0083`，
    报告里应该出现"缺失"和百分比，而不是 `{'null_ratio': 0.0083}`。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id, "检查数据集中的缺失值")
    run_workflow(state)

    assert state.report is not None
    text = " ".join(section.text for section in state.report.sections)
    assert "缺失" in text
    assert "%" in text
    # 原始 dict 的痕迹不该出现在给用户看的句子里
    assert "null_ratio" not in text
    assert "{" not in text


def test_build_report_notes_come_from_the_profile(basket):
    """数据质量说明直接来自画像的 warnings，不是报告层自己算的。"""
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.report is not None
    assert state.profile is not None
    assert state.profile.warnings[0] in state.report.data_quality_notes


def test_report_is_json_serializable(basket):
    """报告必须能真的序列化成 JSON 再发给前端。

    `ToolResult.data` 里的数字来自 pandas/numpy，报告层把它们搬进 `table` 时
    很容易带进 `np.float64` 这类"看起来像 float"的东西 —— 那种值
    `json.dumps` 会直接报 `TypeError`，而且是发响应那一刻才炸。
    """
    _, dataset_id = basket
    state = _make_state(dataset_id)
    run_workflow(state)

    assert state.report is not None
    dumped = json.dumps(state.report.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
    assert "分析报告" in dumped


# ---------------------------------------------------------------------------
# 5. 边界：不许越界
# ---------------------------------------------------------------------------


def _imported_roots(module_file: str) -> set[str]:
    """扫出源码里 import 的**顶层包名**（`from app.llm import client` → `app`）。"""
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_orchestrator_does_not_import_the_web_framework():
    """编排层不许 import fastapi/starlette（架构文档 §3.3）。

    理由不是"洁癖"：`run_workflow` 是要在**后台线程**里跑的。一旦它开始
    依赖请求上下文（`Request`、`BackgroundTasks`），它就只能从路由里调用，
    以后想加 CLI 入口、想写离线批处理，全部要重写。
    """
    imported = _imported_roots(orchestrator_module.__file__)
    assert "fastapi" not in imported
    assert "starlette" not in imported


def test_orchestrator_does_not_talk_to_the_model_directly():
    """编排层不许直接 import `app.llm` —— 它只能通过 `planner.plan()` 间接用模型。

    架构文档 §3 统一约定第 3 条：只有 `planner` 和 `validation.build_report` 能调 LLM。
    如果编排层自己也能发请求，"谁在什么时候花了钱、超时算谁的"就没人说得清了。
    """
    imported = _imported_roots(orchestrator_module.__file__)
    assert "app.llm" not in imported
    assert "openai" not in imported
