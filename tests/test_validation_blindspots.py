"""盲区回归：D5 校验层的四个"看起来在工作、其实放行了编造数字"的洞。

**这个文件存在的理由**：D-22 第 3 条说"注释里的前提一样会骗人，改完必须证明它
能重新变红"。I-1..I-4 这四条都是**注释声称已经防住、实测却防不住**的类型 ——
它们不是"少写了一个 if"，是"尺子本身量错了"。所以每一条都必须有一条用例，
在把修复回退掉的时候**真的变红**。没有这层用例，将来有人"顺手简化"
`_same_magnitude` 或把扫描范围收回去，CI 不会响。

四条盲区（编号沿用 `.tmp/LEAD_I2_FINDING.md` 与 task-26 的记账）：

- **I-1** 模板报告自己举报自己：全任务失败时那段兜底文案里写了"计划里一共 N 个
  任务"，而 N 读的是 `state.plan.tasks` —— 计划不在任何 `ToolResult` 里，
  于是 `passed=False unsupported=['2']`。报告诚实地承认失败，却被打成"编造数字"。
- **I-1b** 同一类的第二处：`_summary` 里"系统一共规划了 N 个分析任务，成功 M 个"。
  它当时没被发现，纯粹因为 `summary` 不在扫描范围内（见 I-3）。
- **I-2** `_summarize_correlation` 的"共比较了 N 对"里 N = `len(valid)`，
  而允许集里对应它的是 `_collect_numbers` 的"某个键有值的条数"——`r` 恰好是
  float `0.0` 时那个键是假值，两边定义不一致，模板自己写的话被判成编造。
  **N 有出处 ⟺ 没有任何一对的 r 正好是 0.0**，这个巧合就是 bug。
- **I-3** `_check_numbers` 只扫 `sections[].text`，`summary` 与
  `data_quality_notes` 里的数字**一个都不查**。往这两处注入编造数字可以随便过。
- **I-4** `_same_number` 无条件 round 到 0/1/2 位再比。`round(x, 0)` 把两个不同的
  数压进同一个整数桶：`_same_number(3.2, 3.0) = True`、
  `(0.4, 12.0) = True`、`(0.3, 40.0) = True`。编任何个位数，
  只要允许集里有个附近整数就蒙混过关。

跑法（项目根）：
    & ".\\.venv\\Scripts\\python.exe" -m pytest tests/test_validation_blindspots.py -q
"""
from __future__ import annotations

import pytest

import app  # noqa: F401  必须先 import app 设好 MPLCONFIGDIR，别让 matplotlib 污染仓库根

from app.agents.validation import _same_number, validate
from app.data.loader import load_dataframe
from app.data.store import DatasetStore
from app.schemas_workflow import (
    ReportSection,
    WorkflowState,
)
from app import config
from app.agents import orchestrator as orchestrator_module
from app.agents.orchestrator import run_workflow

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"
WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
DATASET_ID = "fedcba9876543210fedcba9876543210"


@pytest.fixture(scope="module")
def sales_df():
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture
def basket(monkeypatch, sales_df, tmp_path):
    """独立 DatasetStore + 显式配置。理由同 `tests/test_validation_numbers.py`。"""
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    monkeypatch.setenv("MAX_WORKFLOW_SECONDS", "180")
    monkeypatch.setenv("TOOL_CALL_LIMIT", "20")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    config.reset_settings_cache()

    fake_store = DatasetStore()
    fake_store.register(DATASET_ID, sales_df, "sales.csv", "csv", 1024, "sha-test")
    monkeypatch.setattr(orchestrator_module, "store", fake_store)
    yield fake_store
    config.reset_settings_cache()


def _run(question: str = "分析不同地区的销售额") -> WorkflowState:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    state = WorkflowState(
        workflow_id=WORKFLOW_ID,
        dataset_id=DATASET_ID,
        question=question,
        created_at=now,
        started_at=now,
    )
    run_workflow(state)
    assert state.report is not None, "工作流应该已经生成了报告"
    return state


# ===========================================================================
# I-1 / I-1b：报告不能自己举报自己
# ===========================================================================


def test_all_tasks_failed_does_not_self_accuse(basket):
    """I-1：全部任务失败时，兜底文案里不能出现没有工具出处的计划计数。

    复现旧行为：把每个任务都标成 FAILED，让 `_template_report` 走"一段结果都没有"
    那条分支。旧代码在这里写 `f"计划里一共 {_planned_count(state)} 个任务，全部失败了。"`，
    `_planned_count` 读 `state.plan.tasks` —— 计划不在任何 `ToolResult` 里，
    所以那个数字永远没有出处，`passed=False unsupported=['2']`。

    这个 bug 的性质很恶劣：报告**恰恰在诚实承认失败**，却被自己的校验层
    判成"编造数字"，网页上呈现为黄色警告条。
    """
    state = _run("分析不同地区的销售额")

    # 让所有结果都失败，逼出"一段结果都没有"的兜底分支。
    # ⚠️ 字段名是 `error_message`，不是 `error`（`ToolResult` 没有 `error` 字段，
    # 写错了 pydantic 会直接抛 `object has no field "error"`）。
    for result in state.results:
        result.ok = False
        result.error_message = "人为制造的失败（本用例要测的是兜底文案）"

    from app.agents.validation import _template_report

    report = _template_report(state)
    state.report = report

    result = validate(state)

    assert result.passed is True, (
        f"诚实承认失败的报告被自己判成编造：unsupported={result.unsupported_numbers}"
    )
    assert result.unsupported_numbers == []
    # 兜底文案确实还在（不能靠"什么都不写"来通过）
    text = " ".join(section.text for section in report.sections)
    assert "没有产生任何成功的结果" in text


def test_summary_does_not_claim_the_planned_task_count(basket):
    """I-1b：`_summary` 里不能写"一共规划了 N 个任务"。

    `len(tasks)` 读 `state.plan.tasks`，同样没有工具出处。它此前一直没红，
    只是因为 `summary` 不在扫描范围内；I-3 扩了范围之后它当场暴露。

    这条用例钉的是**句子本身**，不是扫描结果 —— 所以它同时能在
    "有人把扫描范围收回去"和"有人把这个数字加回 summary"两种回退下变红。
    """
    state = _run("分析不同地区的销售额")

    assert "规划了" not in state.report.summary, (
        "summary 里不该出现'规划了几个任务'这种没有工具出处的计数"
    )
    # 但"成功几个"要留下：它有出处（成功结果条数）
    assert "成功完成" in state.report.summary

    result = validate(state)
    assert result.passed is True
    assert result.unsupported_numbers == []


# ===========================================================================
# I-2：相关性"共比较了 N 对"必须有显式出处，不能靠 r != 0.0 的巧合
# ===========================================================================


def test_correlation_reports_an_explicit_pair_count(basket):
    """I-2：`correlation` 要显式给出计数字段，报告直接读它。

    旧实现报告说"共比较了 N 对"，N = `len(valid)`；而允许集里对应它的
    是 `_collect_numbers` 的"某个键有值的条数"（`r` 非 None **且非零**）。
    两边一致**当且仅当没有任何一对的 r 正好是 float 0.0**。
    """
    import app.tools.compare as compare

    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    data = compare.correlation(df, {})

    assert "pair_count" in data, "correlation 必须显式返回 pair_count"
    assert "valid_pair_count" in data, "correlation 必须显式返回 valid_pair_count"
    assert data["pair_count"] == len(data["pairs"])
    assert data["valid_pair_count"] == sum(1 for p in data["pairs"] if p["r"] is not None)
    assert data["valid_pair_count"] <= data["pair_count"]


def test_zero_correlation_pair_does_not_break_the_honest_count(basket):
    """I-2 的决定性用例：**存在 r 正好为 0.0 的列对时，模板自己写的话不能被判成编造。**

    复现条件（唯一真正的复现条件）：让某一对数值列的 Pearson r 正好落在 0.0。
    构造成 x=[1,2,3,4]、y=[1,-1,-1,1]：两组都零均值、协方差 0 → r 恰好 0.0。

    旧实现的机制（本用例逐条钉住，所以它不是"凭感觉"的断言）：
      - 报告层数的是 `len(valid)` = 3（三对都算出了 r）；
      - 校验层的允许集里对应它的只有"键 `r` 有值的条数"，
        而 `_collect_numbers` 用 `if node[0].get(key)` 判真假 —— `0.0` 是假值，
        所以那一对**不算**，数出来是 2；
      - 于是报告里诚实的"共比较了 3 对"被判成 `UNSUPPORTED_NUMBER`。
    修法是让 `correlation` 显式返回 `valid_pair_count`，报告直接读它 —— 两边同源。
    """
    import pandas as pd

    from app.agents.validation import _collect_numbers, _summarize_correlation

    # x 与 y 严格正交：x 取值 [1,2,3,4]，y 取值 [1,-1,-1,1]，
    # 两组都零均值，协方差 0 → Pearson r 正好 0.0
    df = pd.DataFrame(
        {
            "x": [1.0, 2.0, 3.0, 4.0],
            "y": [1.0, -1.0, -1.0, 1.0],
            "z": [1.0, 2.0, 4.0, 8.0],
        }
    )
    assert df["x"].corr(df["y"]) == 0.0, "构造失败：这两列的 r 必须正好是 0.0"

    import app.tools.compare as compare

    data = compare.correlation(df, {})

    # 工具显式给出的计数：三对都算出了 r（包括那一对 0.0）
    assert data["valid_pair_count"] == 3
    assert data["pair_count"] == 3

    # 钉住旧口径的**确切**行为：数"键 r 有值的条数"会得到 2，不是 3。
    # 这不是猜的 —— 直接按 `_collect_numbers` 的规则手算一遍对照。
    truthy_r = sum(1 for pair in data["pairs"] if pair.get("r"))
    assert truthy_r == 2, "0.0 那一对确实是假值，所以旧口径少数了一对"
    assert truthy_r != data["valid_pair_count"], (
        "两个口径在这份数据上必须不一致 —— 一致的话本用例就没测到 I-2"
    )

    # 而校验层的隐式计数也确实给出 2（与手算一致）
    implicit: set[float] = set()
    _collect_numbers(data, implicit)
    assert 2.0 in implicit, "隐式计数该把 r 有值的条数 2 也收进来"

    # 报告读的是显式字段，所以它说的是真话
    text = _summarize_correlation(data)
    assert "共比较了 3 对" in text, f"报告该说 3 对，实际：{text!r}"


def test_zero_correlation_still_validates(basket):
    """I-2 端到端：`r == 0.0` 的列对在场时，报告仍须 `passed=True`。

    上面那条钉的是 `_summarize_correlation` 的文本；这条钉的是它真的能过校验
    （也就是 `valid_pair_count` 进了允许集）。
    """
    import pandas as pd

    from app.schemas_workflow import AnalysisTask, TaskPlan, TaskStatus, ToolResult
    from datetime import datetime, timezone

    df = pd.DataFrame(
        {"x": [1.0, 2.0, 3.0, 4.0], "y": [1.0, -1.0, -1.0, 1.0], "z": [1.0, 2.0, 4.0, 8.0]}
    )
    import app.tools.compare as compare

    data = compare.correlation(df, {})

    state = _run("分析不同地区的销售额")
    # 用这份人造的相关性结果替换掉真实结果。
    # `ToolResult` 的必填字段照抄 `tests/test_validation_numbers.py` 的造法。
    now = datetime.now(timezone.utc)
    state.results = [
        ToolResult(
            task_id="T2",
            tool_name="correlation",
            ok=True,
            data=data,
            error_message=None,
            elapsed_ms=1,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
    ]
    state.plan = TaskPlan(
        plan_id="plan-t2",
        question=state.question,
        dataset_id=DATASET_ID,
        source="rule_fallback",
        created_at=now,
        tasks=[
            AnalysisTask(
                task_id="T2",
                order=1,
                kind="correlation",
                tool_name="correlation",
                params={},
                description="算相关系数",
                status=TaskStatus.SUCCESS,
            )
        ],
    )
    state.report = None  # 让 _template_report 用新结果重建

    from app.agents.validation import _template_report

    state.report = _template_report(state)
    result = validate(state)

    assert "共比较了 3 对" in " ".join(s.text for s in state.report.sections)
    assert result.unsupported_numbers == [], (
        f"诚实的计数被判成编造：{result.unsupported_numbers}"
    )


# ===========================================================================
# I-3：summary 与 data_quality_notes 必须在扫描范围内
# ===========================================================================


def test_fabricated_number_in_summary_is_caught(basket):
    """I-3 上半：往 `summary` 里塞一个编造的数字 → 必须红。

    旧行为：`_check_numbers` 只遍历 `report.sections`，summary **一个字都不查**，
    所以 `passed=True checked=1 unsupported=[]`。
    """
    state = _run("分析不同地区的销售额")

    state.report.summary = state.report.summary + "\n\n本次分析的退货率高达 88.8%。"
    result = validate(state)

    assert result.passed is False, "summary 里的编造数字被放行了（I-3）"
    assert "88.8" in result.unsupported_numbers


def test_fabricated_number_in_quality_notes_is_caught(basket):
    """I-3 下半：往 `data_quality_notes` 里塞一个编造的数字 → 必须红。

    旧行为：同样一个字都不查。这一处比 summary 更值得测 ——
    质量提示是最容易被"顺手加一句统计"的地方。
    """
    state = _run("分析不同地区的销售额")

    state.report.data_quality_notes = list(state.report.data_quality_notes) + [
        "另外还有 77.7% 的异常值需要关注"
    ]
    result = validate(state)

    assert result.passed is False, "质量提示里的编造数字被放行了（I-3）"
    assert "77.7" in result.unsupported_numbers


def test_the_profile_is_a_legitimate_number_source(basket):
    """I-3 的必要配套：画像字段必须算作"数字的出处"。

    `data_quality_notes` 里每一句都来自画像（"有 5 列存在缺失值"、
    "检测到 8 行完全重复的记录"），而画像**不是** `ToolResult` ——
    旧 `_allowed_numbers(state.results)` 根本收不到它。
    所以"扩扫描范围"与"把画像算进出处"必须同批做：
    只扩范围会当场把诚实的质量提示打成编造（实测多出 `['8','2160']` 两条红线）。
    """
    state = _run("分析不同地区的销售额")

    assert state.profile is not None
    result = validate(state)

    assert result.passed is True, (
        f"诚实的质量提示被误伤：unsupported={result.unsupported_numbers}"
    )
    assert result.unsupported_numbers == []
    # 质量提示确实非空（不能靠"没有提示"来通过）
    assert state.report.data_quality_notes, "这份数据该有质量提示，不然本用例空转"
    assert result.checked_numbers > 0


def test_quality_notes_numbers_all_trace_to_the_profile(basket):
    """把 `data_quality_notes` 里的每个数字单独拿出来，都必须有出处。

    这条比"整份报告 passed"更细：它定位到具体的字段，
    所以在"有人往质量提示里加了一句带数字的话、而那个数字碰巧被别处的
    允许集覆盖了"这种情况下也能提示出来。
    """
    from app.agents.validation import _NUMBER_RE, _allowed_numbers, _to_float, _is_supported

    state = _run("分析不同地区的销售额")
    allowed = _allowed_numbers(state)

    untraceable: list[str] = []
    for note in state.report.data_quality_notes:
        for raw in _NUMBER_RE.findall(note):
            value = _to_float(raw)
            if value is None:
                continue
            if not _is_supported(value, allowed):
                untraceable.append(f"{raw}  <- {note}")

    assert untraceable == [], "质量提示里有找不到出处的数字：\n" + "\n".join(untraceable)


# ===========================================================================
# I-4：容差不能宽到把不同的数当成同一个
# ===========================================================================


# (报告里写的, 数据里真实的) —— 必须**拦下**：不同量级 / 编造的数字
I4_MUST_BLOCK = [
    (1.0, 1.4),  # 相差 0.4，旧实现 round 到 0 位 = 1.0 vs 1.0
    (0.4, 12.0),  # 相差 30 倍
    (0.3, 40.0),  # 相差 133 倍
    (3.2, 3.0),  # I-4 的真实成因：编的 3.2 蹭上真实的 3.0
    (47.5, 999.0),
    (5.0, 5.9),
    (88.8, 0.0083),  # 编造的百分比
    (99.99, 99.9),  # 见 test_i4_rounding_to_integers_still_cannot_launder
]

# (报告里写的, 数据里真实的) —— 必须**放行**：同一个事实的不同写法
I4_MUST_ALLOW = [
    (24871657.0, 24871657.0),  # 千分位去掉之后是同一个数
    (3.4999, 3.5),  # 四舍五入到一位小数的正当容差
    (1.1, 0.0111),  # _percent：0.0111 写成 1.1%
    (0.8, 0.0083),  # _percent：0.0083 写成 0.8%
    (43.1, 0.43130055299151465),  # _percent 的长小数
    (1.0, 0.01),  # ×100 换算
    (8.0, 8.0),
    (2160.0, 2160.0),
    (0.0, 0.0),  # 0 只跟 0 是同一件事
]


@pytest.mark.parametrize(("report_value", "data_value"), I4_MUST_BLOCK)
def test_i4_blocks_numbers_of_a_different_magnitude(report_value, data_value):
    """I-4：量级不同的两个数不能算"同一个事实的两种写法"。"""
    assert _same_number(report_value, data_value) is False, (
        f"_same_number({report_value}, {data_value}) 应为 False —— "
        f"否则编造的数字只要蹭上附近任意一个整数就能过关"
    )


@pytest.mark.parametrize(("report_value", "data_value"), I4_MUST_ALLOW)
def test_i4_allows_the_honest_writings(report_value, data_value):
    """I-4 的反面：收紧容差不能把诚实的写法一起误杀。

    这一组是"不能简单粗暴地去掉 round"的证据：`24,871,657.00` ↔ `24871657.0`
    在真实报告里天天出现，百分比互换是 `_percent` 的基本行为。
    """
    assert _same_number(report_value, data_value) is True, (
        f"_same_number({report_value}, {data_value}) 应为 True —— "
        f"收紧容差误杀了诚实的写法"
    )


def test_i4_rounding_to_integers_still_cannot_launder(basket):
    """I-4 的第二层：**量级闸门通过之后**，round 到 0 位依然会误判。

    这条用例是被变异测试逮出来的。`.tmp/mutation_d26.py` 的 I4b 变异把
    `for digits in (1, 2)` 放回 `(0, 1, 2)`、其余不动（量级闸门保留），
    结果 26 条用例**全绿** —— 说明"闸门 + 小数位"这套组合在这个输入类上没被钉住。

    反例只在"相对差 < 0.1% 但两数跨了整数边界"时出现：

        _same_magnitude(99.99, 99.9) is True      # 相对差 0.09%，放行
        round(99.99, 0) == round(99.9, 0)         # 100.0 == 100.0 → 得 True
        round(99.99, 1) != round(99.9, 1)         # 100.0 != 99.9  → 得 False

    所以 `99.99` 与 `99.9` 本来是两个不同的数，靠 0 位这一个桶就能说成"同一个"。
    量级闸门把这类反例从"任意两个整数"缩到了"整数边界附近"，但没有消灭它。
    """
    from app.agents.validation import _same_magnitude

    # 先证明这条输入真的走过了量级闸门，否则本用例测的是别的东西
    assert _same_magnitude(99.99, 99.9) is True, (
        "99.99 与 99.9 的相对差应当能通过量级闸门，不然这条用例钉错了位置"
    )
    assert _same_number(99.99, 99.9) is False, (
        "round 到 0 位会把 99.99 与 99.9 都压成 100.0，必须保留到 1 位才能区分开"
    )


def test_i4_end_to_end_a_nearby_integer_does_not_launder_a_fabrication(basket):
    """I-4 端到端：编造的个位数蹭不上真实的整数。

    这份 sales.csv 的允许集里有 `3.0`（某列统计量）。旧实现下
    `_same_number(3.2, 3.0)` 为 True，所以"增长了 3.2 亿"能蒙混过关 ——
    同一条句子里 `47.5` 被抓住、`3.2` 却放行，这就是 I-4 的现场。
    """
    from app.agents.validation import _allowed_numbers

    state = _run("分析不同地区的销售额")
    allowed = _allowed_numbers(state)

    # 确认这份数据的允许集里确实有 3.0（否则本用例测不到那个巧合）
    assert 3.0 in allowed, "这份数据的允许集里该有 3.0，不然本用例空转"

    state.report.sections.append(
        ReportSection(
            heading="模型补充的结论",
            text="本次分析的利润率达到了 47.5%，比上季度增长了 3.2 亿。",
            cites=["T1"],
            table=None,
        )
    )
    result = validate(state)

    assert result.passed is False
    assert "47.5" in result.unsupported_numbers
    assert "3.2" in result.unsupported_numbers, (
        "3.2 蹭上了允许集里的 3.0 —— I-4 的容差还是太宽"
    )
