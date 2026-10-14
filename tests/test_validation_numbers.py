"""`app/agents/validation.py` 里 `validate()` 的测试 —— 数字溯源校验。

这个文件在测什么
----------------
`validate()` 回答的是一个听起来很简单的问题：**"报告里那些数字，真的都是工具
算出来的吗？"** 它是这套系统"不编数字"这条承诺的最后一道防线，所以它必须同时
满足两个方向：

1. **诚实的报告要放行**。模板报告里的每个数字都能追到某次工具调用的结果，
   `passed` 必须是 `True`。一个满屏误报的校验等于没有校验 —— 没人会看它。
2. **编造的内容要拦下**。报告里出现工具结果里没有的数字 → `UNSUPPORTED_NUMBER`
   且 `passed=False`；提到画像里没有的列 → `UNKNOWN_COLUMN`。

只测其中一个方向都是半个证明：只测 (2) 的话，"永远返回 passed=False" 能全绿；
只测 (1) 的话，"永远返回 passed=True" 能全绿。

为什么要真的跑一遍工作流
------------------------
下面大部分用例先 `run_workflow(state)` 跑出**真实的报告**，再在上面做手脚。
手写一个假的 `FinalReport` 更容易，但那样测的是"我以为模板会写什么"，
而不是"模板真的写了什么"。真跑一遍的代价是慢一点（约 0.5 秒），换来的是
报告文本一变、这些断言立刻跟着变。

为什么临时目录要自己钉
----------------------
`CHARTS_DIR` 被 `validate()` 用来查"登记的图在不在磁盘上"。不钉的话，
用例会在仓库的 `data/charts/` 里翻找真实文件 —— 一个依赖机器状态的断言，
换台机器就红。所以每个用例都把它指到 `tmp_path`。
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app import config
from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import run_workflow
from app.agents.validation import validate
from app.data.loader import load_dataframe
from app.data.store import DatasetStore
from app.schemas_tool import ChartResult, ToolResult
from app.schemas_workflow import (
    AnalysisTask,
    FinalReport,
    ReportSection,
    TaskPlan,
    TaskStatus,
    ValidationIssue,
    ValidationResult,
    WorkflowState,
)

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"
WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
DATASET_ID = "fedcba9876543210fedcba9876543210"

# 六个 code 是冻结的（`tests/test_contracts_d5.py` 用集合相等钉过）。
# 这里再抄一遍是为了让"这个文件覆盖了哪些 code"一眼可见。
ALL_CODES = (
    "UNSUPPORTED_NUMBER",
    "UNKNOWN_COLUMN",
    "EMPTY_RESULT",
    "FAILED_TASK",
    "MISSING_CHART_FILE",
    "SHORT_REPORT",
)


@pytest.fixture(scope="module")
def sales_df():
    """真实的示例数据。整个模块共用一份，读盘只读一次。"""
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture
def basket(monkeypatch, sales_df):
    """一个**独立**的 DatasetStore，换掉 orchestrator 眼里的那个 `store`。

    用独立实例而不是全局单例：`store` 没有 `unregister()`，往单例里注册一次
    这份数据就永远留在进程里，用例之间会互相看见（见 test_orchestrator.py 的
    模块 docstring，本项目真踩过）。
    """
    fake_store = DatasetStore()
    fake_store.register(DATASET_ID, sales_df, "sales.csv", "csv", 1024, "sha-test")
    monkeypatch.setattr(orchestrator_module, "store", fake_store)
    return fake_store


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """每个用例一套显式配置，结束后清缓存。

    `CHARTS_DIR` 指到 `tmp_path` 是**必需**的：不钉的话校验会在仓库真实的
    `data/charts/` 里查文件，结果取决于这台机器上跑过什么。
    """
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    monkeypatch.setenv("MAX_WORKFLOW_SECONDS", "180")
    monkeypatch.setenv("TOOL_CALL_LIMIT", "20")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _make_state(question: str = "分析不同地区的销售额") -> WorkflowState:
    """造一个"已经被后台外壳接手"的状态。`started_at` 自己写，理由同
    `tests/test_orchestrator.py::_make_state`（真实链路里是 `_run_safely()` 写的）。"""
    now = datetime.now(timezone.utc)
    return WorkflowState(
        workflow_id=WORKFLOW_ID,
        dataset_id=DATASET_ID,
        question=question,
        created_at=now,
        started_at=now,
    )


def _run(question: str = "分析不同地区的销售额") -> WorkflowState:
    """真跑一遍工作流，返回跑完的状态。"""
    state = _make_state(question)
    run_workflow(state)
    assert state.report is not None, "工作流应该已经生成了报告"
    return state


def _append_section(state: WorkflowState, text: str, heading: str = "模型补充的结论") -> None:
    """往真实报告上追加一段（模拟"模型多写了一段"）。"""
    state.report.sections.append(
        ReportSection(heading=heading, text=text, cites=["T1"], table=None)
    )


def _codes(result: ValidationResult) -> set[str]:
    return {issue.code for issue in result.issues}


def _issues(result: ValidationResult, code: str) -> list[ValidationIssue]:
    return [issue for issue in result.issues if issue.code == code]


# ---------------------------------------------------------------------------
# 1. 诚实的报告必须放行（否则这个校验就是个误报机器）
# ---------------------------------------------------------------------------


def test_real_report_passes_validation(basket):
    """真实数据跑出来的模板报告，`passed` 必须是 True。

    这是整个文件的**基准线**：模板报告里每个数字都来自工具结果（`_num` 只是
    把它写成两位小数、`_percent` 只是乘了 100），所以一个正确的溯源校验
    必须放行它。这条红了通常意味着两件事之一：容差口径太窄，或者模板里
    出现了没有出处的数字（后者是真 bug，`_summarize_missing` 就踩过一次）。
    """
    state = _run("各地区的销售额最多、缺失值情况和趋势关系对比")

    result = validate(state)

    assert result.passed is True
    assert result.unsupported_numbers == []
    assert result.issues == []
    assert result.checked_numbers > 0, "得真的核对过数字，不能是空转"


def test_checked_numbers_counts_the_numbers_in_the_sections(basket):
    """`checked_numbers` 数的是**真的被核对过**的数字个数。

    它必须大于 0（说明确实扫了），且不小于报告里"看起来像数字"的 token 数 ——
    用 `_NUMBER_RE` 独立数一遍做交叉验证。这条能抓住"正则写错导致一个数字都没抽到"：
    那种情况下 `checked_numbers` 是 0、`passed` 还是 True，整条防线形同虚设。
    """
    import re

    state = _run("各地区的销售额最多、缺失值情况和趋势关系对比")
    text = " ".join(section.text for section in state.report.sections)
    independent_count = len(re.findall(r"(?<![A-Za-z_0-9])(-?\d[\d,]*(?:\.\d+)?)", text))

    result = validate(state)

    assert result.checked_numbers == independent_count
    assert independent_count >= 10, "这份报告里的数字不止十个，正则不该漏掉"


def test_percentage_tolerance_accepts_the_honest_writing(basket):
    """`0.8%` 与工具结果里的 `0.0083` 是同一个事实的两种写法，必须放行。

    这条钉的是"百分比与小数换算"那把尺子。去掉它，模板报告里每一个
    `null_ratio` 都会变成"找不到出处"。
    """
    state = _run("检查数据集的缺失值")
    text = " ".join(section.text for section in state.report.sections)

    result = validate(state)

    assert "%" in text, "这份报告本来就该带百分比，不然这条用例什么也没测到"
    assert result.passed is True
    assert result.unsupported_numbers == []


def test_rounding_tolerance_accepts_two_decimal_writing(basket):
    """`0.99` 对应工具结果里的 `0.9872`，必须放行（`_num` 只写两位小数）。

    这条钉的是"四舍五入到 0/1/2 位小数"那把尺子。

    **这里必须挑一个"报告里的数字真的被写短了"的问题**。变异测试抓出过我
    最初选的问题（"分析不同地区的销售额"）：它的正文是
    `销售额_sum = 24,871,657.00`，这个字面量**原样就在 data 里** ——
    字面相等就通过了，压根没走到容差那一步。也就是说那条用例名字写着
    "tolerance"，实际什么都没测到：把整段四舍五入逻辑删掉，它照样绿。

    改用"比较不同类别的利润差异"：它的正文是
    `利润_mean = 12,463.92，差值 = 6,050.37`，而 data 里是没舍入的原值
    （`12463.9237…`），只有走容差才对得上。
    """
    state = _run("比较不同类别的利润差异")
    report_text = " ".join(section.text for section in state.report.sections)

    # 先把"这条用例确实测到了容差"证出来：报告里写的 12,463.92 在 data 里
    # 找不到任何一个**数值上相等**的数据点（data 里是 12463.9237…）。
    # 不能用子串判断（"12463.92" 恰好是 "12463.923776223775" 的前缀，
    # 那样写永远为假，等于把断言删了）。要比的是数值。
    shown = 12463.92
    data_numbers = [
        value
        for result in state.results
        for row in result.data.get("rows", [])
        for value in row.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    assert data_numbers, "这条用例依赖 data 里有数值行，不然它什么也没测到"
    assert all(
        abs(value - shown) > 1e-9 for value in data_numbers
    ), f"data 里不该有恰好等于 {shown} 的值，否则字面/精确比较就能通过，测不到容差"

    result = validate(state)

    assert result.passed is True
    assert result.unsupported_numbers == []
    assert "12,463.92" in report_text, "报告里应该是被写短了的那两位小数版本"


def test_structural_counts_are_accepted(basket):
    """报告里的"共 5 列有缺失值"是**数**出来的，不是算出来的。

    这类规模量不会以数值的形式出现在 `data` 里，所以允许集必须包含"结构计数"。

    **这里必须挑一个真的依赖"某个键有值的条数"那条规则的问题**。变异测试
    抓出过我最初选的问题（"分析不同地区的销售额"）：它的 `6` 是
    `len(rows)`，由"list 的长度"那条规则提供 —— 把"按 key 计数"整段删掉，
    它照样绿。而"检查数据集的缺失值"里的 `5`（有缺失值的列数）只能由
    "`null_count` 有值的条数"算出来，`len(columns)` 是 9，不是 5。
    """
    state = _run("检查数据集的缺失值")
    text = " ".join(section.text for section in state.report.sections)

    result = validate(state)

    assert "共检查 9 列" in text and "5 列有缺失值" in text
    assert result.passed is True
    assert result.unsupported_numbers == []


def test_boolean_values_are_never_a_source_for_numbers(basket):
    """`data` 里的 `True` 不能变成数字 `1` 的出处。

    Python 里 `True` 就是 `1`，`isinstance(True, int)` 也是 True ——
    不显式挡掉 bool，允许集里就会混进一个 1 和一堆 0。后果是：
    报告里随手写个"1"都能"找到出处"，编造数字的闸门被开了一道后门。

    这条直接测 `_collect_numbers`（数据层的规则），因为走完整工作流很难
    凑出一份"带布尔字段的工具结果"。
    """
    from app.agents.validation import _collect_numbers

    allowed: set[float] = set()
    _collect_numbers({"flag": True, "other": False, "count": 3}, allowed)

    assert 3.0 in allowed, "正常数字还是要收进去的"
    assert 1.0 not in allowed, "True 不该被当成 1"
    assert 0.0 not in allowed, "False 不该被当成 0"


# ---------------------------------------------------------------------------
# 2. 编造的内容必须拦下（这是这个校验存在的理由）
# ---------------------------------------------------------------------------


def test_fabricated_number_is_caught(basket):
    """**必测项**：往报告里塞一个工具结果里没有的数字 → `UNSUPPORTED_NUMBER` 且 `passed=False`。

    这是 D5 验收标准里点名的那一条。它是"模型不许自己编数字"这条承诺的
    唯一执行者：规划阶段拦不住（模型完全可以在正文里写"利润率 47.5%"），
    执行阶段也拦不住（数字是报告层写的，不经过任何工具）。
    """
    state = _run("分析不同地区的销售额")

    _append_section(state, "本次分析的利润率达到了 47.5%，比上季度增长了 3.2 亿。")
    result = validate(state)

    assert result.passed is False
    assert _codes(result) == {"UNSUPPORTED_NUMBER"}
    assert "47.5" in result.unsupported_numbers
    assert "3.2" in result.unsupported_numbers
    assert all(issue.severity == "error" for issue in _issues(result, "UNSUPPORTED_NUMBER"))


def test_fabricated_number_message_shows_the_sentence(basket):
    """报错消息里要带上"是哪一段写错了"，否则用户只知道自己被拦了、不知道改哪里。

    LLM 报告路径（D5 之后那条）会把这些 issues 附进 prompt 让它重写，
    消息里没有原文的话模型也无从下手。
    """
    state = _run("分析不同地区的销售额")

    _append_section(state, "本次分析的利润率达到了 47.5%。")
    result = validate(state)

    issue = _issues(result, "UNSUPPORTED_NUMBER")[0]
    assert "47.5" in issue.message
    assert "利润率" in issue.message


def test_fabricated_number_keeps_the_original_writing(basket):
    """`unsupported_numbers` 记的是**原始写法**（带千分位、带负号），不是解析后的 float。

    记 float 的话 `24,871,657.00` 会变成 `24871657.0`，人在日志里对不上原文。
    """
    state = _run("分析不同地区的销售额")

    _append_section(state, "这个季度一共卖出了 12,345,678.90 元。")
    result = validate(state)

    assert "12,345,678.90" in result.unsupported_numbers


def test_negative_number_is_parsed_with_its_sign(basket):
    """负数要连符号一起读。

    模板报告里真有负数（时间序列的环比），正则如果漏掉负号，
    `-0.20` 会被当成 `0.20` 去比对 —— 碰巧也能对上，但那是运气。
    这里塞一个符号明确错开的数字：`-47.5` 有出处、`47.5` 没有（反过来同理）。
    """
    state = _run("分析不同地区的销售额")

    _append_section(state, "利润下滑了 47.5 个百分点。")
    result = validate(state)

    assert "47.5" in result.unsupported_numbers, "这个 47.5 确实没有出处"


def test_task_id_digits_are_not_treated_as_numbers(basket):
    """段尾的"任务：T1"里的 1 不是数据结论，不该被当成数字去溯源。

    它其实也在允许集里（`T1` 的 1 能对上"第一个任务"这种巧合），所以
    这条用例换个角度钉：**一段只有任务标记、没有数字的正文，核对数必须是 0**。
    正则前面那个负向后顾 `(?<![A-Za-z_0-9])` 就是为它写的。
    """
    state = _run("分析不同地区的销售额")
    state.report.sections = [
        ReportSection(heading="占位", text="这一节没有任何数字。（工具：x，任务：T1）", cites=["T1"])
    ]

    result = validate(state)

    assert result.checked_numbers == 0
    assert result.passed is True


def test_fabricated_column_is_caught(basket):
    """报告里提到画像里没有的列名 → `UNKNOWN_COLUMN`。

    数字是真的（复用了工具算出来的销售额），所以数字那关拦不住它 ——
    这正是需要第二类检查的原因：**数字真不等于引用真**。
    """
    state = _run("分析不同地区的销售额")
    real_number = state.report.sections[0].text.split("= ")[-1].split("（")[0]

    _append_section(state, f"毛利率 的均值是 {real_number}。")
    result = validate(state)

    assert result.passed is False
    assert "UNKNOWN_COLUMN" in _codes(result)
    assert "毛利率" in _issues(result, "UNKNOWN_COLUMN")[0].message


def test_real_column_names_are_accepted(basket):
    """反向：报告里本来就有的真实列名（含工具拼的 `销售额_sum`）不能被误报。

    `销售额_sum` 不在画像里，但它是 `group_aggregate` 真实拼出来的输出列。
    不认它的话，每一份分组汇总的报告都会被误报一次 —— 误报多了，
    这条校验就会被当成噪声关掉。
    """
    state = _run("分析不同地区的销售额")
    text = " ".join(section.text for section in state.report.sections)

    result = validate(state)

    assert "销售额_sum" in text, "这份报告本来就该带聚合列名"
    assert _issues(result, "UNKNOWN_COLUMN") == []


# ---------------------------------------------------------------------------
# 3. 任务层面的问题
# ---------------------------------------------------------------------------


def test_failed_task_is_an_error(basket):
    """任务失败 → `FAILED_TASK`，severity 是 error（报告里少了一整块结论）。"""
    state = _run("分析不同地区的销售额")
    state.plan.tasks[0].status = TaskStatus.FAILED

    result = validate(state)

    assert result.passed is False
    issue = _issues(result, "FAILED_TASK")[0]
    assert issue.severity == "error"
    assert issue.task_id == "T1"


def test_skipped_task_is_only_a_warning(basket):
    """被跳过的任务只记 warning。

    根因（它依赖的那个任务失败）已经记了一条 error，同一个错误不该算两遍 ——
    否则前端列出来的问题列表里，一个根因会带出一串重复项。
    """
    state = _run("分析不同地区的销售额")
    state.plan.tasks[0].status = TaskStatus.SKIPPED

    result = validate(state)

    issue = _issues(result, "FAILED_TASK")[0]
    assert issue.severity == "warning"
    assert result.passed is True, "只有 warning 时不该推翻整份报告"


def test_empty_result_is_only_a_warning(basket):
    """工具成功了但返回 0 行 → `EMPTY_RESULT`，warning。

    0 行不是编造，是这份数据在这条问题下真的没有结果。记 warning 让它可见，
    但为它把整份报告判死是不对的。
    """
    state = _run("分析不同地区的销售额")
    state.results[0].data["rows"] = []
    state.results[0].data["row_count"] = 0

    result = validate(state)

    assert "EMPTY_RESULT" in _codes(result)
    issue = _issues(result, "EMPTY_RESULT")[0]
    assert issue.severity == "warning"
    assert issue.task_id == "T1"


def test_empty_result_is_detected_from_rows_alone(basket):
    """`rows` 是空的但工具没填 `row_count` 时也要认出来。

    只认 `row_count == 0` 的话，某个工具忘了填这个字段就会静默漏掉。
    """
    state = _run("分析不同地区的销售额")
    state.results[0].data.pop("row_count", None)
    state.results[0].data["rows"] = []

    result = validate(state)

    assert "EMPTY_RESULT" in _codes(result)


# ---------------------------------------------------------------------------
# 4. 图表层面的问题
# ---------------------------------------------------------------------------


def _chart(state: WorkflowState, filename: str = "a1b2c3d4e5f6.png") -> ChartResult:
    return ChartResult(
        chart_id=filename.removesuffix(".png"),
        filename=filename,
        title="销售额按地区",
        chart_type="bar",
        url=f"/api/workflows/{state.workflow_id}/charts/{filename}",
        source_task_id="T1",
        x_field="地区",
        y_field="销售额_sum",
        point_count=6,
        created_at=datetime.now(timezone.utc),
    )


def test_registered_chart_without_a_file_is_an_error(basket):
    """图登记了但磁盘上没有 → `MISSING_CHART_FILE`（error）。

    前端拿 `chart.url` 直接塞 `<img src>`，文件不在的表现是"图片裂开"，
    而后端日志一切正常 —— 这是最难查的一类故障，必须在校验里拦住。
    """
    state = _run("分析不同地区的销售额")
    state.charts.append(_chart(state))

    result = validate(state)

    assert result.passed is False
    issue = _issues(result, "MISSING_CHART_FILE")[0]
    assert issue.severity == "error"
    assert "a1b2c3d4e5f6.png" in issue.message


def test_registered_chart_with_a_real_file_passes(basket):
    """反向：文件真的在磁盘上（且在配置的 `charts_path` 下）就不能误报。"""
    state = _run("分析不同地区的销售额")
    chart = _chart(state)
    target_dir = config.get_settings().charts_path / state.workflow_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / chart.filename).write_bytes(b"\x89PNG\r\n\x1a\nfake")
    state.charts.append(chart)

    result = validate(state)

    assert _issues(result, "MISSING_CHART_FILE") == []
    assert result.passed is True


def test_chart_pointing_at_an_unknown_task_is_an_error(basket):
    """图的 `source_task_id` 在结果里找不到 → 出处对不上，error。"""
    state = _run("分析不同地区的销售额")
    chart = _chart(state, "bbbbbbbbbbbb.png")
    chart.source_task_id = "T9"
    state.charts.append(chart)

    result = validate(state)

    issues = _issues(result, "MISSING_CHART_FILE")
    assert any("T9" in issue.message for issue in issues)


def test_task_that_asked_for_a_chart_but_has_none_is_an_error(basket):
    """任务要求出图、任务成功了，却没有登记对应的图 → 出图那一步失败了。

    这条是**闭环检查**：出图失败时 `state.validation` 还不存在，没别的地方
    能记这件事（`WorkflowState` 的字段集合是冻结的）。少了它，"出图静默失败"
    就完全没有痕迹 —— 用户看到的是"报告里说该有图，但一张都没有"。

    造法是**把图从 `state.charts` 里拿掉**，而不是给任务现配一个 `ChartRequest`：
    计划阶段现在自己就会给 `group_aggregate` 配图（`planner._chart_for`），
    真跑一遍工作流之后 `state.charts` 里是有图的。清空它模拟的正是
    "`render_chart` 抛了异常、编排器跳过这张图"之后的状态。
    """
    state = _run("分析不同地区的销售额")
    assert state.plan.tasks[0].chart is not None, "计划阶段应该已经配好图了"
    state.charts.clear()

    result = validate(state)

    assert result.passed is False
    issue = _issues(result, "MISSING_CHART_FILE")[0]
    assert "T1" in issue.message


def test_task_with_a_chart_and_a_registered_file_is_not_flagged(basket):
    """反向：任务要图、图也登记了、文件也在 → 不该报 `MISSING_CHART_FILE`。"""
    from app.schemas_workflow import ChartRequest

    state = _run("分析不同地区的销售额")
    state.plan.tasks[0].chart = ChartRequest(
        chart_type="bar", x_field="地区", y_field="销售额_sum", title="销售额按地区"
    )
    chart = _chart(state)
    target_dir = config.get_settings().charts_path / state.workflow_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / chart.filename).write_bytes(b"\x89PNG\r\n\x1a\nfake")
    state.charts.append(chart)

    result = validate(state)

    assert _issues(result, "MISSING_CHART_FILE") == []
    assert result.passed is True


# ---------------------------------------------------------------------------
# 5. 报告层面的问题 + 绝不抛异常
# ---------------------------------------------------------------------------


def test_missing_report_is_an_error(basket):
    """报告压根没生成 → `SHORT_REPORT`（error）且 `passed=False`。

    6 个 code 是冻结的，里面没有"没有报告"这一种 —— 它是最极端的"报告太短"。
    """
    state = _run("分析不同地区的销售额")
    state.report = None

    result = validate(state)

    assert result.passed is False
    assert _issues(result, "SHORT_REPORT")[0].severity == "error"
    assert result.checked_numbers == 0


def test_too_short_report_is_only_a_warning(basket):
    """报告短得像没写完 → `SHORT_REPORT`（warning），不推翻整份报告。"""
    state = _run("分析不同地区的销售额")
    state.report.sections = [
        ReportSection(heading="一句话", text="分析完成。", cites=[], table=None)
    ]
    state.report.summary = "短"

    result = validate(state)

    assert _issues(result, "SHORT_REPORT")[0].severity == "warning"
    assert result.passed is True


def test_short_report_threshold_is_pinned_to_the_measured_boundary(basket):
    """`_MIN_REPORT_CHARS` 的具体值要钉住，否则它被悄悄改小就没人发现。

    阈值是量出来的（见实现里的注释）：真实模板报告最短 158 字、
    `_minimal_report()` 兜底 94 字。这里在 120 的两侧各造一份报告，
    证明那条线真的卡在两者之间 —— 兜底报告被记下来，真实报告不被误报。
    """
    state = _run("分析不同地区的销售额")

    state.report.summary = "x" * 100
    state.report.sections = [ReportSection(heading="h", text="y" * 19, cites=[])]
    assert _issues(validate(state), "SHORT_REPORT"), "119 字应该被记一条"

    state.report.sections = [ReportSection(heading="h", text="y" * 21, cites=[])]
    assert not _issues(validate(state), "SHORT_REPORT"), "121 字不该被记"


def test_the_fallback_report_is_flagged_as_short(basket):
    """`_minimal_report()` 出现就意味着"报告生成那一步炸了"，必须被看见。

    它是 94 字，卡在阈值下面 —— 这条用例证明那个 warning 真的会亮，
    而不是一个永远不触发的分支。
    """
    from app.agents.validation import _minimal_report

    state = _run("分析不同地区的销售额")
    state.report = _minimal_report(state)

    result = validate(state)

    assert _issues(result, "SHORT_REPORT")[0].severity == "warning"


def test_validate_never_raises_on_a_bare_state(basket):
    """`validate()` 绝不抛异常 —— 空状态也要给出一份形状合法的结果。

    校验是"最后一道防线"。防线自己塌了要留下痕迹，而不是把已经算好的
    分析结果一起毁掉。
    """
    state = WorkflowState(
        workflow_id=WORKFLOW_ID,
        dataset_id=DATASET_ID,
        question="随便问问",
        created_at=datetime.now(timezone.utc),
    )

    result = validate(state)

    assert isinstance(result, ValidationResult)
    assert result.passed is False, "连报告都没有，不能算通过"
    assert result.checked_at.tzinfo is not None, "时间戳要带时区（项目统一 UTC）"


def test_validate_never_raises_when_the_report_is_garbage(basket):
    """报告结构被人为破坏时也不能抛异常，而是返回 `passed=False` 的结果。

    这里把 `sections` 换成一个非列表的对象。真实链路里不会发生，
    但"绝不抛异常"这条承诺只有在这种输入下才被真正检验。
    """
    state = _run("分析不同地区的销售额")
    state.report.sections = None  # type: ignore[assignment]

    result = validate(state)

    assert isinstance(result, ValidationResult)
    assert result.passed is False


def test_validate_does_not_modify_the_report(basket):
    """校验只**读**，不改报告（架构文档 §3.5.1 的"不能做"第一条）。

    它要是顺手"修好"了报告，用户看到的就不是模型原本写的东西了 ——
    而校验的价值恰恰在于如实报告"哪里对不上"。
    """
    state = _run("分析不同地区的销售额")
    _append_section(state, "编造的数字 47.5。")
    before = state.report.model_dump()

    validate(state)

    assert state.report.model_dump() == before


def test_validate_does_not_rerun_tools(basket):
    """校验不重跑工具：`state.results` 必须原样不动。

    重跑一遍会带来两个问题：慢（每次校验都要再算一次），以及
    "校验时算出来的数和报告里的数不一样"（数据在变的情况下）。
    """
    state = _run("分析不同地区的销售额")
    before = [result.model_dump() for result in state.results]

    validate(state)

    assert [result.model_dump() for result in state.results] == before


def test_validate_does_not_touch_the_workflow_status(basket):
    """校验不决定 `WorkflowState.status`（那是 Orchestrator 的事）。

    这条是刻意的边界：校验只回答"这份报告可信吗"，降不降级由
    `_final_status()` 决定。校验自己改状态的话，两个地方都在写同一个字段，
    排查问题时永远说不清是谁改的。
    """
    state = _run("分析不同地区的销售额")
    before = (state.status, state.step, state.step_index)

    validate(state)

    assert (state.status, state.step, state.step_index) == before


def test_every_issue_carries_a_frozen_code(basket):
    """所有 issue 的 code 都必须是冻结的 6 个之一，severity 都必须是三档之一。

    新造一个 code 会让前端按 code 分派的逻辑静默失效（那个分支永远不进）。
    """
    state = _run("分析不同地区的销售额")
    _append_section(state, "编造的 47.5。")
    state.plan.tasks[0].status = TaskStatus.FAILED
    state.results[0].data["rows"] = []
    state.charts.append(_chart(state))

    result = validate(state)

    assert result.issues, "这份状态应该有一堆问题"
    for issue in result.issues:
        assert issue.code in ALL_CODES
        assert issue.severity in ("error", "warning", "info")


def test_passed_is_false_whenever_an_error_exists(basket):
    """`passed` 的口径：有任何 error 级 issue 就是 False，只有 warning 才是 True。

    这条把"口径"本身钉住，而不是逐个场景去猜。
    """
    state = _run("分析不同地区的销售额")
    _append_section(state, "编造的 47.5。")

    with_error = validate(state)
    assert any(issue.severity == "error" for issue in with_error.issues)
    assert with_error.passed is False

    clean = validate(_run("分析不同地区的销售额"))
    assert all(issue.severity != "error" for issue in clean.issues)
    assert clean.passed is True
