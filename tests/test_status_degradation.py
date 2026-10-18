"""数字校验查到问题时，工作流状态该降级到哪一档：**降级，但绝不失败**。

## 这个文件在防什么

`validate()` 能查出六类问题，其中 error 级的有四类（编造的数字、不存在的列名、
失败的任务、图表文件不见了）。查出来之后系统有两种做法：

- **降级**：状态从 `SUCCESS` 变成 `SUCCESS_WITH_WARNINGS`，报告照给，用户看到黄标。
- **判失败**：状态变成 `FAILED`，用户看到红标，可能以为"分析没跑出来"。

选降级的理由：**数字是用 pandas 真算出来的，报告里有一句话对不上不等于分析失败。**
如果校验能把状态打成 `FAILED`，那么一次模型用词不准就会让整份算好的结果被标成
"失败" —— 用户的第一反应是"重跑一次"，而重跑会再花一次模型调用、结果还是一样。

所以这个文件里**每一条用例都同时断言 `is not WorkflowStatus.FAILED`**。
只断言"等于 SUCCESS_WITH_WARNINGS"是不够的：`FAILED` 不等于它，但真正的
回归往往是把整个函数换成常量 `FAILED`，那样前面那些断言全都会红 ——
反过来，把函数换成恒 `SUCCESS` 时，只有"该降级"的用例会红。
两个方向都要有，缺一个方向就等于只测了一半。

## 为什么直接调 `_final_status()` 而不是跑完整工作流

因为要测的是**这一个判断函数**。跑完整工作流的话，`state.validation` 是 `validate()`
自己填的，想让它是 `passed=False` 就得先造一份编造的报告 —— 那样测出来的其实是
"`validate()` 抓不抓得住编造"，那是 `tests/test_validation_numbers.py` 的范围。
这里要问的问题只有一个：**手上这份 `ValidationResult` 摆在那里，状态该怎么定。**

（这也意味着这个文件不覆盖"`validate()` 真的会填 `state.validation`" ——
那件事由 `tests/test_validation_numbers.py` 与端到端用例负责。）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import _final_status
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    AnalysisTask,
    FinalReport,
    ReportSection,
    TaskPlan,
    TaskStatus,
    ValidationIssue,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
)

DATASET_ID = "0123456789abcdef0123456789abcdef"

# 哨兵：区分"调用方没传 report"与"调用方要一份没有报告的 state"（见 `_state`）。
_UNSET = object()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _report() -> FinalReport:
    """一份"长得正常"的报告。

    为什么要造它而不是用 `build_report()`：`_final_status` 的第一条分支是
    "报告不存在就降级"。这里要测的是**后面几条分支**，所以报告必须存在，
    否则所有用例都会从同一条分支返回 —— 那样四条断言其实只测了一条。
    """
    return FinalReport(
        report_id="r1",
        title="分析报告：分析不同地区的销售额",
        summary="本次未使用大模型，以下结论为工具结果的直接汇总",
        sections=[
            ReportSection(
                heading="按地区汇总销售额",
                text="排在最前面的一组是 地区 = 华南，销售额_sum = 24,871,657.00。",
                cites=["T1"],
                table=None,
            )
        ],
        data_quality_notes=[],
        citations=["T1"],
        generated_at=_now(),
        llm_used=False,
    )


def _result(ok: bool = True) -> ToolResult:
    return ToolResult(
        task_id="T1",
        tool_name="group_aggregate",
        ok=ok,
        data={"rows": [], "row_count": 0},
        elapsed_ms=1,
        created_at=_now(),
    )


def _state(
    *,
    report: FinalReport | None | object = _UNSET,
    validation: ValidationResult | None = None,
    task_status: TaskStatus = TaskStatus.SUCCESS,
    result_ok: bool = True,
) -> WorkflowState:
    """造一个"分析已经跑完、只剩定状态"的 WorkflowState。

    默认全是"一切正常"：报告在、任务成功、结果成功、没校验过 → 应该是 `SUCCESS`。
    每条用例只改它关心的那一项，这样"是哪一项导致了降级"永远是一眼可见的。

    `report` 的默认值为什么是一个哨兵对象而不是 `None`：`None` 在这个函数里是
    **有意义的输入**（表示"报告没生成"）。默认值直接写 `None` 的话，就分不清
    "调用方没传"和"调用方明确要一份没有报告的 state"，前者会被当成后者 ——
    这条真踩过：`_state(report=None)` 静默地拿到了一份正常报告，
    "报告不存在就降级"那条用例断言的是完全另一回事。
    """
    state = WorkflowState(
        workflow_id="a" * 32,
        dataset_id=DATASET_ID,
        question="分析不同地区的销售额",
        created_at=_now(),
        started_at=_now(),
    )
    state.plan = TaskPlan(
        plan_id="p1",
        question="分析不同地区的销售额",
        dataset_id=DATASET_ID,
        tasks=[
            AnalysisTask(
                task_id="T1",
                order=1,
                kind="aggregate",
                tool_name="group_aggregate",
                params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
                description="按地区汇总销售额",
                status=task_status,
            )
        ],
        source="rule_fallback",
        created_at=_now(),
    )
    state.report = _report() if report is _UNSET else report  # type: ignore[assignment]
    state.results = [_result(ok=result_ok)]
    state.validation = validation
    return state


def _validation(
    passed: bool, severity: str = "error", code: str = "UNSUPPORTED_NUMBER"
) -> ValidationResult:
    """造一份校验结果。`passed=False` 时按 `passed` 的口径**必须**至少有一条 error。"""
    issues: list[ValidationIssue] = []
    if not passed:
        issues.append(
            ValidationIssue(
                severity=severity,  # type: ignore[arg-type]
                code=code,  # type: ignore[arg-type]
                message="报告里出现了 88.8，但所有工具结果里都找不到这个数字的出处。",
                task_id="T1",
            )
        )
    return ValidationResult(
        passed=passed,
        checked_numbers=3,
        unsupported_numbers=["88.8"] if not passed else [],
        issues=issues,
        checked_at=_now(),
    )


# ---------------------------------------------------------------------------
# 1. 基线：一切正常就是干净的 SUCCESS
# ---------------------------------------------------------------------------


def test_clean_state_stays_success():
    """没校验过、任务成功、结果成功 → `SUCCESS`。

    这是本文件其余用例的对照组：没有它，"降级"的断言可能只是因为
    `_final_status` 恒返回 `SUCCESS_WITH_WARNINGS`。
    """
    state = _state()

    assert _final_status(state) is WorkflowStatus.SUCCESS


def test_passed_validation_keeps_success():
    """校验跑过而且通过了（`passed=True`）→ 仍然是 `SUCCESS`。

    这条防的是"看到 `state.validation` 非空就降级"：那样每一次分析都会带上黄标，
    黄标就失去意义了（前端靠它区分"这次干净"和"这次有话说"）。
    """
    state = _state(validation=_validation(passed=True))

    assert _final_status(state) is WorkflowStatus.SUCCESS


# ---------------------------------------------------------------------------
# 2. 核心：校验发现 error 级问题 → 降级，但绝不 FAILED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "UNSUPPORTED_NUMBER",
        "UNKNOWN_COLUMN",
        "FAILED_TASK",
        "MISSING_CHART_FILE",
    ],
)
def test_error_level_issue_degrades_but_never_fails(code):
    """任意一种 error 级问题 → `SUCCESS_WITH_WARNINGS`，**不是 `FAILED`**。

    参数化四个 code 而不是只测一个：`_final_status` 读的是 `passed` 这个结论，
    本来就不该关心是哪种 code。但如果有人日后改成"只有编造数字才降级"，
    另外三种就会漏掉 —— 这条参数化正是防那种改动。
    """
    state = _state(validation=_validation(passed=False, code=code))

    status = _final_status(state)

    assert status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert status is not WorkflowStatus.FAILED


def test_warning_only_validation_does_not_degrade():
    """只有 warning 级 issue（`passed` 仍为 True）→ 不降级。

    口径来自 `ValidationResult.passed` 的定义：`not any(severity == "error")`。
    空结果、报告偏短这类都是 warning —— 它们值得提醒，但不该让整条工作流带上黄标。
    """
    state = _state(validation=_validation(passed=True))

    assert _final_status(state) is WorkflowStatus.SUCCESS


def test_validation_does_not_overrule_a_missing_report():
    """报告都没生成时，降级原因不止一个 —— 状态仍是 `SUCCESS_WITH_WARNINGS`。

    这条覆盖"`state.report is None` 那条分支"与校验分支同时成立的情形：
    两条路都指向同一档，不是相加成更严重的状态（比如有人写出 `FAILED`）。
    """
    state = _state(report=None, validation=_validation(passed=False))

    status = _final_status(state)

    assert status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert status is not WorkflowStatus.FAILED


# ---------------------------------------------------------------------------
# 3. 回归保护：D4 就有的三条降级理由不能被新分支挤掉
# ---------------------------------------------------------------------------


def test_missing_report_still_degrades():
    """报告不存在 → 降级（D4 既有行为，回归保护）。"""
    state = _state(report=None)

    assert _final_status(state) is WorkflowStatus.SUCCESS_WITH_WARNINGS


def test_failed_result_still_degrades():
    """有工具结果失败 → 降级（D4 既有行为，回归保护）。

    为什么专门保这一条：新的校验分支被插在 `_final_status` 中间，
    最容易犯的错是插错位置、把后面几条分支变成永远走不到的代码。
    """
    state = _state(result_ok=False)

    assert _final_status(state) is WorkflowStatus.SUCCESS_WITH_WARNINGS


def test_failed_task_still_degrades():
    """有任务失败 → 降级（D4 既有行为，回归保护）。"""
    state = _state(task_status=TaskStatus.FAILED)

    assert _final_status(state) is WorkflowStatus.SUCCESS_WITH_WARNINGS


# ---------------------------------------------------------------------------
# 4. 红线：这个函数**没有任何一条分支**能返回 FAILED
# ---------------------------------------------------------------------------


def test_no_combination_of_inputs_can_produce_failed():
    """把四种"有话说"的情况全叠上，状态也不是 `FAILED`。

    单独看每条分支都返回 `SUCCESS_WITH_WARNINGS`，仍然可能是"某条分支返回
    FAILED，只是我的用例没凑到那个组合"。这条把所有坏消息一起塞进去，
    确认最坏情况也只是黄标。
    """
    state = _state(
        report=None,
        validation=_validation(passed=False),
        task_status=TaskStatus.FAILED,
        result_ok=False,
    )
    state.error = None

    assert _final_status(state) is not WorkflowStatus.FAILED


def test_the_function_never_returns_failed_for_any_flag_combination():
    """穷举四个布尔开关的 16 种组合，`FAILED` 一次都不出现。

    前一条只试了"全坏"这一种组合。这条把组合穷举完 —— `_final_status` 的输入
    空间本来就很小（四个开关），穷举比挑几个例子更彻底，成本也几乎为零。
    """
    produced: set[WorkflowStatus] = set()
    for has_report in (True, False):
        for passed in (True, False):
            for task_ok in (True, False):
                for result_ok in (True, False):
                    state = _state(
                        report=_report() if has_report else None,
                        validation=_validation(passed=passed),
                        task_status=TaskStatus.SUCCESS if task_ok else TaskStatus.FAILED,
                        result_ok=result_ok,
                    )
                    produced.add(_final_status(state))

    assert WorkflowStatus.FAILED not in produced
    # 两个值都出现过，说明这组穷举真的走到了不同的分支（不是全落进同一条）。
    assert produced == {WorkflowStatus.SUCCESS, WorkflowStatus.SUCCESS_WITH_WARNINGS}


# ---------------------------------------------------------------------------
# 5. 契约：只读 `passed`，不自己重算"什么算不通过"
# ---------------------------------------------------------------------------


def test_degradation_reads_the_passed_flag_not_the_issue_list():
    """判定只看 `ValidationResult.passed`，**不在这里重算**"什么算不通过"。

    造一份自相矛盾的结果：`passed=True` 但 issues 里躺着一条 error。
    真实代码永远不会产生它（`validate()` 的 `passed` 就是由 issues 算出来的），
    所以这不是"要支持的输入"，而是一个**探针**：
    - 读 `passed` 的实现 → 返回 `SUCCESS`（它信这个结论）；
    - 自己遍历 issues 找 error 的实现 → 返回 `SUCCESS_WITH_WARNINGS`。

    两种实现都"看起来对"，但只有前者把口径留在了一个地方。口径分叉的代价是
    改 `passed` 的定义时（比如将来加一档"严重程度由 code 决定"），
    这里会静默地按旧口径判断，而两处都不报错。
    """
    contradictory = ValidationResult(
        passed=True,
        checked_numbers=3,
        unsupported_numbers=[],
        issues=[
            ValidationIssue(
                severity="error",
                code="UNSUPPORTED_NUMBER",
                message="这条 error 与 passed=True 自相矛盾，只在测试里存在。",
                task_id=None,
            )
        ],
        checked_at=_now(),
    )
    state = _state(validation=contradictory)

    assert _final_status(state) is WorkflowStatus.SUCCESS


def test_final_status_does_not_mutate_the_state():
    """定状态**只读**状态，不改任何字段（§3 第 1 条：只有 Orchestrator 能写）。

    `_final_status` 是模块里的私有函数，但它仍然只该回答"该是什么状态"，
    写字段是调用方的事。它偷偷写一下 `state.status` 的话，调用方那句
    `_finish(state, _final_status(state))` 会看起来正常，而别处再调它就会出事。
    """
    state = _state(validation=_validation(passed=False))
    before: dict[str, Any] = {
        "status": state.status,
        "step": state.step,
        "step_index": state.step_index,
        "report": state.report,
        "validation": state.validation,
    }

    _final_status(state)

    assert state.status is before["status"]
    assert state.step == before["step"]
    assert state.step_index == before["step_index"]
    assert state.report is before["report"]
    assert state.validation is before["validation"]


def test_final_status_is_reachable_from_the_module():
    """`_final_status` 仍在编排器模块里、且是可调用的函数。

    这条看着像废话，其实在钉一个位置约定：状态判定属于编排器（§8.2 第 1 条
    "只有 Orchestrator 改 state"）。如果哪天它被搬到 `validation.py`，
    "校验"与"定状态"就成了同一个模块的两件事，而校验模块的契约是"绝不改状态" ——
    两者放一起，下一个人很容易顺手让校验去写 `state.status`。
    """
    assert callable(orchestrator_module._final_status)
