"""D3 契约测试：`ToolResult` / `ChartResult` / 工作流模型。

这个文件为什么存在
------------------
第 3 天我们把"工具跑完长什么样""一条工作流长什么样"冻结成了两个模型文件
（`app/schemas_tool.py` / `app/schemas_workflow.py`），然后**三个人对着它并行写代码**：
工具层（算）、执行层（调度）、文档层（描述）。

契约冻结最怕的不是"写错"，而是**写错了却没人发现**：三个人各自按自己的理解写，
代码能跑、测试能过，直到联调那天才发现两边说的不是一回事。
所以这个文件专门钉住三件容易漂移的事：

1. **序列化**：`ToolResult` / `ChartResult` 必须能过 `model_dump(mode="json")`
   且 `json.dumps(..., allow_nan=False)` 成功。这条是"接口响应永远不会因为
   序列化失败而 500"的实现。
2. **`ChartResult` 的 filename / url / chart_id 三者一致**：这条约定已经被写进
   模型的 `model_validator`，所以测试要验证的是"它真的会拦住错的"，而不是
   "我们碰巧每次都传对了"。
3. **`WorkflowState` 的派生字段与限额**：`has_report` 与 `report` 的关系、
   `retry_count` 的上限、`WorkflowStatusData.from_state()` 不泄漏内部字段。

测试里刻意用**真实中文列名**（`销售额`/`地区`），因为这一整套系统的数据
就是中文的，用 `col_a` 之类的假名测不出编码问题。
"""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import ApiError, ErrorCode
from app.schemas_dataset import DatasetProfile
from app.schemas_tool import ChartResult, ToolResult
from app.schemas_workflow import (
    TERMINAL_STATUSES,
    AnalysisTask,
    FinalReport,
    QuestionRequest,
    ReportSection,
    TaskPlan,
    TaskStatus,
    ToolResult as _ToolResultReexport,
    WorkflowState,
    WorkflowStatus,
    WorkflowStatusData,
)


def _now() -> datetime:
    """统一用带时区的 UTC 时间。项目里不许出现裸 `datetime.now()`。"""
    return datetime.now(timezone.utc)


def _tool_result(**overrides: object) -> ToolResult:
    """造一个合法的 `ToolResult`，测试只覆盖自己关心的字段。"""
    payload: dict[str, object] = {
        "task_id": "T1",
        "tool_name": "group_aggregate",
        "ok": True,
        "data": {"columns": ["地区", "销售额_sum"], "rows": [{"地区": "华东", "销售额_sum": 123.5}], "row_count": 1},
        "columns": ["地区", "销售额_sum"],
        "row_count": 1,
        "elapsed_ms": 3,
        "created_at": _now(),
    }
    payload.update(overrides)
    return ToolResult(**payload)  # type: ignore[arg-type]


def _chart_result(**overrides: object) -> ChartResult:
    chart_id = "a1b2c3d4e5f6"
    payload: dict[str, object] = {
        "chart_id": chart_id,
        "filename": f"{chart_id}.png",
        "title": "各地区销售额",
        "chart_type": "bar",
        "url": f"/api/workflows/{'0' * 32}/charts/{chart_id}.png",
        "source_task_id": "T1",
        "x_field": "地区",
        "y_field": "销售额_sum",
        "point_count": 6,
        "created_at": _now(),
    }
    payload.update(overrides)
    return ChartResult(**payload)  # type: ignore[arg-type]


def _workflow_state(**overrides: object) -> WorkflowState:
    payload: dict[str, object] = {
        "workflow_id": "a" * 32,
        "dataset_id": "b" * 32,
        "question": "分析不同地区的销售额",
        "created_at": _now(),
    }
    payload.update(overrides)
    return WorkflowState(**payload)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. ToolResult
# --------------------------------------------------------------------------


def test_tool_result_survives_json_dump() -> None:
    """工具结果必须能直接进 JSON —— 这是接口不会 500 的前提。

    `allow_nan=False` 是关键：默认的 `json.dumps` 会把 NaN 写成非法的 `NaN` 字面量，
    浏览器 `JSON.parse` 会直接报错。所以整条链路都用这个参数当裁判。
    """
    import json

    result = _tool_result()
    dumped = result.model_dump(mode="json")
    text = json.dumps(dumped, allow_nan=False, ensure_ascii=False)

    assert "华东" in text  # 中文没有被转义成 \uXXXX
    assert dumped["task_id"] == "T1"
    assert dumped["created_at"].endswith("Z") or "+00:00" in dumped["created_at"]


def test_tool_result_rejects_wrong_scalar_types() -> None:
    """契约不是文档里的提醒，是会报错的代码。

    `elapsed_ms` 与 `row_count` 都是 `int` 且有 `ge=0`：传小数或负数必须当场被拒。
    如果这条测试红了，说明有人把字段类型放宽了 —— 那会让"耗时 1.5 毫秒"这种
    只在联调时才发现的怪数据流进报告。
    """
    with pytest.raises(ValidationError):
        _tool_result(elapsed_ms=1.5)

    with pytest.raises(ValidationError):
        _tool_result(row_count=-1)

    with pytest.raises(ValidationError):
        _tool_result(ok="maybe")  # 见下一条测试：哪些字符串会被当成 True


def test_pydantic_lax_mode_is_recorded_not_assumed() -> None:
    """记录 Pydantic 宽松模式（lax mode）的真实边界 —— 别凭印象写断言。

    这条测试的来历：我最初在这里断言 `ToolResult(ok="yes")` 会被拒，**实测没抛**。
    在改断言之前我先跑了探针（`.tmp/probe_pydantic_lax.py`，pydantic 2.13.4）
    把真实行为逐条打出来，结果是：

        bool 字段接受：True / False / 1 / 0 / "yes" / "no" / "true" / "false"
                       / "on" / "off" / "1" / "0"
        bool 字段拒绝："maybe" / "" / 2 / -1 / None / 1.5
        int  字段接受：1 / 1.0 / "3" / True
        int  字段拒绝：1.5 / "abc" / None

    为什么把这个"怪行为"写进测试而不是绕开：
    ① **测试要断言事实，不能断言印象**。写 `raises(ValidationError)` 之前必须
       真的试过它会抛，否则就是把测试改成迁就实现。
    ② 这个行为有实际影响：`ok` 字段是**我们自己**的代码填的（不是用户输入），
       所以 `"yes"` 被当成 `True` 不会造成安全问题；但如果哪天有人把
       `ToolResult` 直接拿去解析外部 JSON（例如模型返回的工具结果），
       宽松转换就意味着 `{"ok": "yes"}` 会被静默接受。这条测试是那个判断的依据。
    ③ `int` 接受 `1.0` 但拒绝 `1.5` —— 这就是 `elapsed_ms` 必须由我们
       `int(round(...))` 自己转的原因：传浮点进去，整数部分侥幸过关、
       带小数的直接被拒，行为不一致最难查。
    """
    # bool：这些字符串会被当成 True / False（Pydantic 宽松模式）
    assert _tool_result(ok="yes").ok is True
    assert _tool_result(ok="no").ok is False
    assert _tool_result(ok=1).ok is True
    assert _tool_result(ok=0).ok is False

    # bool：这些真的会被拒
    for bad in ("maybe", "", 2, -1, None, 1.5):
        with pytest.raises(ValidationError):
            _tool_result(ok=bad)

    # int：整数形式的浮点侥幸过关（这就是我们要自己 round 的原因）
    assert _tool_result(row_count=2.0).row_count == 2

    # int：带小数、非数字字符串、None 一律拒绝
    for bad_count in (1.5, "abc", None):
        with pytest.raises(ValidationError):
            _tool_result(row_count=bad_count)


def test_tool_result_rejects_unknown_error_code() -> None:
    """`error_code` 是 5 选 1 的 Literal，写错一个字母就该被拦下。

    为什么重要：前端是按 `error_code` 查中文提示表的，出现一个没人认识的码，
    用户看到的就是空白。宁可在这里炸。
    """
    with pytest.raises(ValidationError):
        _tool_result(ok=False, error_code="OOPS", error_message="随便写的")


@pytest.mark.parametrize(
    "code",
    ["TOOL_NOT_FOUND", "BAD_PARAMS", "COLUMN_NOT_FOUND", "BAD_OPERATION", "BAD_RESULT"],
)
def test_every_tool_error_code_is_accepted(code: str) -> None:
    """5 个码都要能通过 —— 与校验 1–5 一一对应，少一个就有一类失败没法表达。"""
    result = _tool_result(ok=False, error_code=code, error_message="说明")
    assert result.error_code == code
    assert result.error_message == "说明"


# --------------------------------------------------------------------------
# 2. ChartResult：三处一致
# --------------------------------------------------------------------------


def test_chart_result_accepts_consistent_triple() -> None:
    chart = _chart_result()
    assert chart.filename == f"{chart.chart_id}.png"
    assert chart.url.endswith(f"/charts/{chart.filename}")


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"filename": "other.png"}, "文件名与 chart_id 不一致"),
        ({"filename": "a1b2c3d4e5f6.jpg"}, "扩展名不是 png"),
        ({"url": "/api/workflows/x/charts/other.png"}, "url 指向的文件名与 filename 不一致"),
        ({"url": "charts/a1b2c3d4e5f6.png"}, "url 少了 /api/workflows 前缀"),
        ({"url": ""}, "url 为空"),
    ],
)
def test_chart_result_rejects_inconsistent_triple(overrides: dict[str, str], reason: str) -> None:
    """这张表就是 `model_validator` 的存在理由。

    图片 404 **不会让任何测试变红** —— 浏览器里只是一张破图，演示时才发现。
    所以这里把每一种"对不上"的形态都钉一条。
    """
    with pytest.raises(ValidationError):
        _chart_result(**overrides)


def test_chart_result_rejects_bad_chart_id_and_type() -> None:
    with pytest.raises(ValidationError):
        _chart_result(chart_id="NOTHEX", filename="NOTHEX.png")  # 必须 12 位小写 hex

    with pytest.raises(ValidationError):
        _chart_result(chart_type="donut")  # 只有 bar / line / pie / scatter


def test_chart_result_survives_json_dump() -> None:
    import json

    dumped = _chart_result().model_dump(mode="json")
    json.dumps(dumped, allow_nan=False, ensure_ascii=False)
    assert dumped["chart_id"] == "a1b2c3d4e5f6"


# --------------------------------------------------------------------------
# 3. 工作流模型
# --------------------------------------------------------------------------


def test_terminal_statuses_are_exactly_the_three_terminal_ones() -> None:
    """终态集合是"前端还要不要继续轮询"的唯一依据。

    写错一个（例如把 `CHARTING` 当成终态）的后果是前端在出图阶段就停止轮询，
    页面永远停在"正在生成图表"。所以这里逐一钉死。
    """
    assert TERMINAL_STATUSES == {
        WorkflowStatus.SUCCESS,
        WorkflowStatus.SUCCESS_WITH_WARNINGS,
        WorkflowStatus.FAILED,
    }
    # 非终态一个都不能混进来
    for status in (
        WorkflowStatus.PENDING,
        WorkflowStatus.PROFILING,
        WorkflowStatus.PLANNING,
        WorkflowStatus.EXECUTING,
        WorkflowStatus.CHARTING,
        WorkflowStatus.REPORTING,
        WorkflowStatus.VALIDATING,
    ):
        assert status not in TERMINAL_STATUSES


def test_workflow_state_defaults_are_honest() -> None:
    """刚创建的工作流：什么都没做，所以每个产物字段都必须是"空"而不是"假数据"。

    "还没到那一步"和"那一步失败了"是两件事：前者是 None / 空列表，
    后者会体现在 status 与 error 上。
    """
    state = _workflow_state()

    assert state.status is WorkflowStatus.PENDING
    assert state.step == "profiling"
    assert state.step_index == 0
    assert state.total_steps == 6
    assert state.profile is None
    assert state.plan is None
    assert state.results == []
    assert state.charts == []
    assert state.report is None
    assert state.validation is None
    assert state.error is None
    assert state.has_report is False
    assert state.retry_count == 0
    assert state.tool_call_count == 0
    assert state.started_at is None
    assert state.finished_at is None


def test_workflow_id_must_be_32_lowercase_hex() -> None:
    """`workflow_id` 会进 URL 和磁盘路径，格式必须严格。

    用 `str(uuid4())`（带连字符、36 位）会被拒 —— 这一点和第 2 天的
    `build_stored_filename` 是同一条规矩：id 是"钥匙"，格式不统一就会出现
    "有的地方能找到、有的地方找不到"。
    """
    with pytest.raises(ValidationError):
        _workflow_state(workflow_id="not-a-hex-id")

    with pytest.raises(ValidationError):
        _workflow_state(workflow_id="A" * 32)  # 大写也不行


def test_retry_count_has_a_hard_ceiling() -> None:
    """`retry_count` 上限 6 是"禁止无限循环"这条要求的落地点。

    没有上限的话，"模型失败 → 重试 → 再失败"可以永远转下去，而这正是用户
    提示词里点名禁止的。
    """
    assert _workflow_state(retry_count=6).retry_count == 6

    with pytest.raises(ValidationError):
        _workflow_state(retry_count=7)

    with pytest.raises(ValidationError):
        _workflow_state(retry_count=-1)


def test_workflow_state_holds_tool_results_and_charts() -> None:
    """产物字段真的能装下 `ToolResult` / `ChartResult`（类型对齐，不是 Any）。"""
    state = _workflow_state(results=[_tool_result()], charts=[_chart_result()])

    assert state.results[0].task_id == "T1"
    assert state.charts[0].chart_id == "a1b2c3d4e5f6"

    # 整条状态也要能进 JSON —— 这是接口 6 / 接口 7 的返回值来源
    import json

    json.dumps(state.model_dump(mode="json"), allow_nan=False, ensure_ascii=False)


def test_workflow_state_rejects_bad_error_object() -> None:
    """顶层失败原因复用 `ApiError`，所以错误码必须是 `ErrorCode` 枚举里的值。"""
    state = _workflow_state(
        status=WorkflowStatus.FAILED,
        error=ApiError(code=ErrorCode.INTERNAL_ERROR, message="分析过程中出现未预期错误"),
    )
    assert state.error is not None
    assert state.error.code is ErrorCode.INTERNAL_ERROR

    with pytest.raises(ValidationError):
        _workflow_state(error={"code": "NOT_A_REAL_CODE", "message": "x"})


def test_task_plan_requires_at_least_one_task_and_caps_at_eight() -> None:
    """计划里 1–8 个任务：0 个会让后面所有阶段无事可做，超过 8 个是模型失控的信号。"""
    task = AnalysisTask(
        task_id="T1",
        order=1,
        kind="aggregate",
        tool_name="group_aggregate",
        params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
        description="按地区汇总销售额",
        created_at=_now(),
    )

    plan = TaskPlan(
        plan_id="deadbeef",
        question="分析不同地区的销售额",
        dataset_id="b" * 32,
        tasks=[task],
        source="rule_fallback",
        created_at=_now(),
    )
    assert plan.source == "rule_fallback"
    assert plan.tasks[0].status is TaskStatus.PENDING

    with pytest.raises(ValidationError):
        TaskPlan(
            plan_id="deadbeef",
            question="q",
            dataset_id="b" * 32,
            tasks=[],
            source="rule_fallback",
            created_at=_now(),
        )

    with pytest.raises(ValidationError):
        TaskPlan(
            plan_id="deadbeef",
            question="q",
            dataset_id="b" * 32,
            tasks=[task] * 9,
            source="rule_fallback",
            created_at=_now(),
        )


def test_question_request_bounds() -> None:
    """接口 5 的请求体：问题太短（"a"）多半是误触，太长（>500）要挡在门外。"""
    assert QuestionRequest(dataset_id="b" * 32, question="分析销售额").max_steps == 6

    with pytest.raises(ValidationError):
        QuestionRequest(dataset_id="b" * 32, question="a")

    with pytest.raises(ValidationError):
        QuestionRequest(dataset_id="b" * 32, question="问" * 501)

    with pytest.raises(ValidationError):
        QuestionRequest(dataset_id="b" * 32, question="分析销售额", max_steps=9)


# --------------------------------------------------------------------------
# 4. WorkflowStatusData：状态接口不泄漏内部字段
# --------------------------------------------------------------------------


def test_status_data_does_not_leak_internal_fields() -> None:
    """接口 6 每秒被调一次，返回体必须小。

    这条测试的真正价值：将来有人给 `WorkflowState` 加一个内部字段（例如
    `debug_trace`），只要它没被加进 `WorkflowStatusData`，API 就不会变。
    直接返回 `WorkflowState` 的做法会让那个字段**自动**出现在响应里。
    """
    state = _workflow_state()
    data = WorkflowStatusData.from_state(state)

    keys = set(data.model_dump().keys())
    assert "results" not in keys  # 可能有几百行表格
    assert "report" not in keys  # 更长
    assert "plan" not in keys
    assert "validation" not in keys
    assert "retry_count" not in keys
    assert "tool_call_count" not in keys
    assert "heartbeat_at" not in keys


def test_status_data_computes_has_report_instead_of_copying_it() -> None:
    """`has_report` 现算，不抄 `state.has_report`。

    为什么这条值得单独测：`has_report` 是派生值，派生值就有"某个分支忘了同步"
    的风险 —— 那个分支只设了 `report`、忘了改 `has_report`，前端就会永远不去取报告，
    页面停在"正在撰写报告"。现算一次，接口的正确性就不再依赖每个写入点都记得同步。

    构造方法：**故意制造一个自相矛盾的状态**（`report` 有值但 `has_report=False`），
    看接口到底听谁的。听 `report` 才对。
    """
    report = FinalReport(
        report_id="abcd1234",
        title="销售额按地区分析",
        summary="华东最高。",
        sections=[ReportSection(heading="结论", text="华东地区销售额最高。")],
        generated_at=_now(),
        llm_used=False,
    )
    state = _workflow_state(report=report, has_report=False)  # 刻意的矛盾状态

    assert state.has_report is False  # 字段本身确实是 False（Pydantic 不会替我们算）
    assert WorkflowStatusData.from_state(state).has_report is True  # 但接口听 report 的


def test_status_data_exposes_only_four_task_fields() -> None:
    """任务在前端只显示四项：id、中文描述、状态、错误。

    **不给 `params`** —— 用户不需要看见工具参数，那是排查时才看的东西；
    而且 `params` 里可能出现列名，等于把内部结构暴露给前端。
    """
    task = AnalysisTask(
        task_id="T1",
        order=1,
        kind="aggregate",
        tool_name="group_aggregate",
        params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
        description="按地区汇总销售额",
        created_at=_now(),
    )
    plan = TaskPlan(
        plan_id="deadbeef",
        question="分析不同地区的销售额",
        dataset_id="b" * 32,
        tasks=[task],
        source="rule_fallback",
        created_at=_now(),
    )
    state = _workflow_state(plan=plan)
    data = WorkflowStatusData.from_state(state)

    assert len(data.tasks) == 1
    brief = data.tasks[0]
    assert set(brief.model_dump().keys()) == {"task_id", "description", "status", "error"}
    assert brief.description == "按地区汇总销售额"


def test_status_data_handles_state_without_plan() -> None:
    """还没规划时（`plan is None`）任务列表是空的，不是崩掉。"""
    data = WorkflowStatusData.from_state(_workflow_state())
    assert data.tasks == []
    assert data.status is WorkflowStatus.PENDING


def test_status_data_survives_json_dump() -> None:
    import json

    data = WorkflowStatusData.from_state(_workflow_state())
    json.dumps(data.model_dump(mode="json"), allow_nan=False, ensure_ascii=False)


# --------------------------------------------------------------------------
# 5. 跨文件一致性：这两条容易在重构时被改坏
# --------------------------------------------------------------------------


def test_workflow_module_reexports_tool_result() -> None:
    """`schemas_workflow` 重新导出了 `ToolResult`，两个名字必须是同一个类。

    为什么要重新导出：读工作流代码的人不该为了看 `state.results` 里装的是什么
    而跳到另一个文件。但如果哪天有人在 `schemas_workflow.py` 里**重新定义**了
    一个同名类，两处就会悄悄变成两个不同的类型，`isinstance` 判断会莫名其妙失败。
    """
    assert _ToolResultReexport is ToolResult


def test_dataset_profile_is_reusable_inside_workflow_state() -> None:
    """画像直接挂在 `WorkflowState.profile` 上，类型必须是第 2 天那个 `DatasetProfile`。"""
    profile = DatasetProfile(
        dataset_id="b" * 32,
        row_count=2168,
        column_count=9,
        columns=[],
        duplicate_row_count=8,
        memory_bytes=12345,
        warnings=["检测到 8 行完全重复的记录"],
        built_at=_now(),
    )
    state = _workflow_state(profile=profile)
    assert state.profile is not None
    assert state.profile.row_count == 2168
    assert state.profile.warnings == ["检测到 8 行完全重复的记录"]
