"""`app/agents/planner.py` 的测试：6 类中文问题都要产出**可执行**的计划。

## 为什么这些断言值得写

规则计划器是"没有大模型时系统仍然可用"的那条路（架构文档 §11 风险 6）。
它的失败方式有两种，都很安静：

- **计划为空**：`TaskPlan` 要求 `tasks` 有 1–8 个，空列表会直接抛 `ValidationError`；
- **列名是编的**：计划看起来很正常，一路跑到执行阶段才报 `COLUMN_NOT_FOUND`，
  而用户看到的会是"工具报错"，真正的原因却是"计划器把 `地区` 硬编码进去了，换个数据集就崩"。

所以这里除了断言"命中了哪条规则"，还要**把每个任务真的过一遍 `validate_call`** ——
那才是"这份计划能跑"的直接证据，而不是"我觉得它能跑"。

数据用真实的 `data/samples/sales.csv`（2168 行 × 9 列），不用手工造的假画像：
假画像很容易刚好符合我们的假设，真实数据的列顺序、中文列名、语义类型判断
才是计划器每天要面对的东西。
"""

from datetime import datetime, timezone
from typing import Any

import pandas as pd
import pytest

from app import config
from app.agents.analysis import run_task
from app.agents.planner import llm_is_configured, plan_with_rules
from app.data.loader import load_dataframe
from app.data.profiler import build_profile
from app.schemas_dataset import DatasetProfile
from app.schemas_workflow import AnalysisTask, TaskPlan
from app.tools import registry
from app.tools.validate import validate_call

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

# 覆盖全部 6 条规则的问题。`describe_dataset` 由"你好"那条覆盖（一个规则都不命中）
QUESTIONS = [
    "检查数据集中的缺失值和重复记录",
    "分析不同地区的销售额",
    "找出销售额最高的十个商品",
    "计算各月份的销售额变化",
    "比较不同类别的利润差异",
    "销售额和利润有什么关系",
    "你好",
]


@pytest.fixture(scope="module")
def sales_df() -> pd.DataFrame:
    """真实样例数据。`load_dataframe` 返回 `(df, 编码)`，这里只要 df。"""
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture(scope="module")
def sales_profile(sales_df: pd.DataFrame) -> DatasetProfile:
    """真实画像。计划器**只**看它，不看数据本身 —— 这正是"不编造列名"的前提。"""
    return build_profile(sales_df, "ds-sales")


def _first_task(plan: TaskPlan, tool_name: str):
    """取计划里第一个用某个工具的任务；没有则返回 `None`（便于写清晰的失败信息）。"""
    for task in plan.tasks:
        if task.tool_name == tool_name:
            return task
    return None


# ---------------------------------------------------------------------------
# 6 类中文问题各一条
# ---------------------------------------------------------------------------


def test_group_aggregate_question(sales_profile: DatasetProfile) -> None:
    """「分析不同地区的销售额」→ `group_aggregate`，指标是**问题里提到的** `销售额`。

    这条同时钉住了两层选列逻辑的第 1 层：`sales.csv` 里第一个数值列是 `销量`，
    如果实现成"永远取第一个数值列"，这里算出来的会是销量 —— 数字正常、
    没有报错、也没有告警，但答的不是用户问的问题。
    """
    plan = plan_with_rules("分析不同地区的销售额", sales_profile)

    task = plan.tasks[0]
    assert task.tool_name == "group_aggregate"
    assert task.params["group_by"] == ["地区"]
    assert task.params["metric"] == "销售额"
    assert task.params["agg"] == "sum"


def test_mentioned_column_wins_over_default_column(sales_profile: DatasetProfile) -> None:
    """两层选列逻辑的**证据**：问「销售额」选销售额，问「销量」选销量。

    只测第一个问题是不够的：默认值恰好是 `销量`，所以"永远取第一个数值列"的实现
    在第一条上会红、但在下面这条上会绿 —— 两条一起测，才把
    "问题里提到的优先"和"没提到才退回默认"两条路径都覆盖住。
    """
    by_sales = plan_with_rules("分析不同地区的销售额", sales_profile)
    by_volume = plan_with_rules("分析不同地区的销量", sales_profile)

    assert by_sales.tasks[0].params["metric"] == "销售额"
    assert by_volume.tasks[0].params["metric"] == "销量"


def test_top_n_question(sales_profile: DatasetProfile) -> None:
    """「找出销售额最高的十个商品」→ `top_n`、`by=销售额`、`n=10`。"""
    plan = plan_with_rules("找出销售额最高的十个商品", sales_profile)

    task = _first_task(plan, "top_n")
    assert task is not None
    assert task.params["by"] == "销售额"
    assert task.params["n"] == 10


def test_missing_values_question(sales_profile: DatasetProfile) -> None:
    """「检查数据集中的缺失值和重复记录」→ `missing_values`，参数为空（它检查所有列）。"""
    plan = plan_with_rules("检查数据集中的缺失值和重复记录", sales_profile)

    task = _first_task(plan, "missing_values")
    assert task is not None
    assert task.params == {}


def test_time_series_question(sales_profile: DatasetProfile) -> None:
    """「计算各月份的销售额变化」→ `time_series_summary`，日期列与指标列都要真实存在。

    这个问题同时含「各」（分组规则）和「月份/变化」（趋势规则）。
    断言的是**第一个**任务：规则表里趋势排在分组之前，所以它该是 T1。
    顺便也证明了"同时命中多条规则会生成多个任务"，而不是只挑一条。
    """
    plan = plan_with_rules("计算各月份的销售额变化", sales_profile)

    task = plan.tasks[0]
    assert task.tool_name == "time_series_summary"
    assert task.params["date_column"] == "日期"
    assert task.params["metric"] == "销售额"
    assert task.params["freq"] == "M"
    assert task.params["include_growth"] is True


def test_compare_question(sales_profile: DatasetProfile) -> None:
    """「比较不同类别的利润差异」→ `compare_groups`，用平均值比较（不是求和）。"""
    plan = plan_with_rules("比较不同类别的利润差异", sales_profile)

    task = _first_task(plan, "compare_groups")
    assert task is not None
    assert task.params["group_by"] == "类别"
    assert task.params["metric"] == "利润"
    assert task.params["agg"] == "mean"


def test_correlation_question(sales_profile: DatasetProfile) -> None:
    """「销售额和利润有什么关系」→ `correlation`（不传列 = 用全部数值列）。"""
    plan = plan_with_rules("销售额和利润有什么关系", sales_profile)

    task = _first_task(plan, "correlation")
    assert task is not None
    assert task.params == {}


def test_unrelated_question_falls_back_to_describe(sales_profile: DatasetProfile) -> None:
    """「你好」→ 一个规则都不命中，仍然产出 `describe_dataset`（**绝不允许空计划**）。

    这条防的是"回一句我不知道你想问什么"。用户问得含糊时，
    给一份描述性统计至少让他看到数据长什么样，比空手而归有用。
    """
    plan = plan_with_rules("你好", sales_profile)

    assert len(plan.tasks) >= 1
    assert plan.tasks[0].tool_name == "describe_dataset"


# ---------------------------------------------------------------------------
# 结构合法性、参数可执行性、不编造列名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("question", QUESTIONS)
def test_plan_is_a_valid_task_plan(question: str, sales_profile: DatasetProfile) -> None:
    """每份计划都过一遍 `TaskPlan.model_validate`，且 `source` 如实写 `rule_fallback`。

    为什么要重新 validate 一次（它本来就是个 `TaskPlan`）：这钉的是
    "计划器造出来的对象**从模型角度**也是合法的"。用 `model_dump()` 再重建，
    等于走了一遍序列化——将来 `TaskPlan` 加了字段、或者计划器往里塞了
    Pydantic 不认识的类型，这里会红，而不是等到 API 序列化响应时才炸。
    """
    plan = plan_with_rules(question, sales_profile)

    rebuilt = TaskPlan.model_validate(plan.model_dump())

    assert rebuilt.source == "rule_fallback"
    assert 1 <= len(rebuilt.tasks) <= 8
    assert [task.order for task in rebuilt.tasks] == list(range(1, len(rebuilt.tasks) + 1))
    assert [task.task_id for task in rebuilt.tasks] == [
        f"T{index}" for index in range(1, len(rebuilt.tasks) + 1)
    ]
    assert rebuilt.dataset_id == sales_profile.dataset_id
    assert rebuilt.question == question


@pytest.mark.parametrize("question", QUESTIONS)
def test_every_task_passes_validation(
    question: str, sales_profile: DatasetProfile, sales_df: pd.DataFrame
) -> None:
    """**每个任务都真的过得了 `validate_call`** —— 这才叫"可执行的计划"。

    只断言"参数里有 `metric` 这个键"是不够的：键在、值错（列名拼错、`agg` 写了个
    不在枚举里的词）同样会让任务在执行阶段失败。这里直接跑一遍校验 1–4，
    让校验层来回答"这份计划能不能跑"。
    """
    plan = plan_with_rules(question, sales_profile)

    for task in plan.tasks:
        spec = validate_call(task.tool_name, task.params, sales_df)  # 抛异常即为失败
        assert spec.name == task.tool_name


@pytest.mark.parametrize("question", QUESTIONS)
def test_plan_never_invents_column_names(question: str, sales_profile: DatasetProfile) -> None:
    """不编造列名：所有"列名语义"的参数值必须在画像里真实存在。

    哪些参数是列名，**从工具自己的 schema 里读**（标了 `column: True` 的那些），
    不在这里手抄一份列名清单 —— 手抄的清单会和 schema 一起腐烂，
    而腐烂的测试比没有测试更糟：它绿灯，但它检查的东西已经不存在了。
    """
    plan = plan_with_rules(question, sales_profile)
    real_columns = {column.name for column in sales_profile.columns}

    for task in plan.tasks:
        spec = registry.get(task.tool_name)
        assert spec is not None, f"计划里出现了注册表里没有的工具：{task.tool_name}"

        for name, rule in spec.params_schema.get("properties", {}).items():
            if not rule.get("column"):
                continue
            value: Any = task.params.get(name)
            if value is None:
                continue
            values = value if isinstance(value, list) else [value]
            for column in values:
                assert column in real_columns, (
                    f"问题「{question}」的任务 {task.task_id} 用了画像里没有的列：{column}"
                )


def test_task_count_respects_max_steps(sales_profile: DatasetProfile) -> None:
    """`max_steps` 是上限，也是**下限保护**：传 0 或 20 都不能把契约顶破。

    `TaskPlan.tasks` 约束是 1–8。传 0 会得到空列表、传 20 会得到 9 个任务，
    两种都会让 `TaskPlan` 直接抛 `ValidationError` —— 而请求里的 `max_steps`
    是用户可控的（`QuestionRequest` 允许 1–8），所以这里必须夹住。
    """
    # 这句话能同时命中趋势、分组、比较三条规则，本来会生成 3 个任务
    question = "按月份变化比较各地区销售额的差异"

    assert len(plan_with_rules(question, sales_profile, max_steps=1).tasks) == 1
    assert len(plan_with_rules(question, sales_profile, max_steps=20).tasks) <= 8
    assert len(plan_with_rules(question, sales_profile, max_steps=0).tasks) == 1


def test_reasoning_mentions_the_matched_rule(sales_profile: DatasetProfile) -> None:
    """`reasoning` 要说清命中了哪条规则 —— 排查"为什么给我算了这个"全靠它。"""
    plan = plan_with_rules("检查数据集中的缺失值和重复记录", sales_profile)

    assert plan.reasoning is not None
    assert "缺失值检查" in plan.reasoning


def test_plan_id_and_created_at_are_fresh(sales_profile: DatasetProfile) -> None:
    """`plan_id` 每次都不一样、`created_at` 是 UTC —— 这两条是复盘时的定位依据。"""
    before = datetime.now(timezone.utc)
    first = plan_with_rules("你好", sales_profile)
    second = plan_with_rules("你好", sales_profile)

    assert first.plan_id != second.plan_id
    assert len(first.plan_id) == 8
    assert first.created_at.tzinfo is not None  # 带时区 = UTC，不是本地时间
    assert first.created_at >= before


# ---------------------------------------------------------------------------
# 缺列时跳过规则（而不是编一个列名出来）
# ---------------------------------------------------------------------------


def test_rules_are_skipped_when_required_columns_are_missing() -> None:
    """画像里没有日期列时，「趋势」规则**跳过**，不编造日期列。

    造一份只有分类列 + 数值列、**没有 datetime 列**的画像，然后问一个只命中趋势规则的问题。
    正确行为是落到 `describe_dataset` 兜底，而不是硬塞一个 `日期` 进去 ——
    那样计划看起来没问题，但执行阶段会报 `COLUMN_NOT_FOUND`，
    而真正的原因（"这个数据集做不了趋势分析"）就被埋掉了。
    """
    profile = DatasetProfile(
        dataset_id="ds-no-date",
        row_count=10,
        column_count=2,
        columns=[
            _column("地区", "categorical"),
            _column("销量", "numeric"),
        ],
        duplicate_row_count=0,
        memory_bytes=1024,
        warnings=[],
        built_at=datetime.now(timezone.utc),
    )

    plan = plan_with_rules("看看销售额的时间走势", profile)

    assert plan.tasks[0].tool_name == "describe_dataset"
    assert all("date_column" not in task.params for task in plan.tasks)


def test_correlation_rule_needs_two_numeric_columns() -> None:
    """只有一个数值列时，「相关」规则跳过：1 列算不出"关系"。"""
    profile = DatasetProfile(
        dataset_id="ds-one-numeric",
        row_count=10,
        column_count=2,
        columns=[
            _column("地区", "categorical"),
            _column("销量", "numeric"),
        ],
        duplicate_row_count=0,
        memory_bytes=1024,
        warnings=[],
        built_at=datetime.now(timezone.utc),
    )

    plan = plan_with_rules("销量和别的列有什么关系", profile)

    assert all(task.tool_name != "correlation" for task in plan.tasks)


def _column(name: str, semantic_type: str):
    """造一个最小的 `ColumnProfile`（只填测试要用的字段，其余走默认值）。"""
    from app.schemas_dataset import ColumnProfile

    return ColumnProfile(
        name=name,
        dtype="object",
        semantic_type=semantic_type,
        null_count=0,
        null_ratio=0.0,
        unique_count=5,
        min_value=None,
        max_value=None,
        top_values=[],
        sample_values=[],
    )


# ---------------------------------------------------------------------------
# llm_is_configured：读的是"此刻"的配置，不是 import 时刻的
# ---------------------------------------------------------------------------


def test_llm_is_configured_reads_settings_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """`llm_is_configured()` 必须在**调用时**读配置，而不是 import 时读一次。

    本项目真踩过这个坑：模块顶层写 `settings = get_settings()`，配置就被绑死在
    import 那一刻，之后改环境变量、`reset_settings_cache()` 全都不起作用
    （见 `docs/DECISIONS.md`）。测试里 monkeypatch 掉 `get_settings` 还能生效，
    就是"它是函数内读的"这件事的证据。

    这里不断言真实配置的值：开发机上 `.env` 配没配模型都可能，
    断言一个具体的 True/False 会让这条用例在别人机器上莫名其妙地红。
    """
    fake = type("FakeSettings", (), {"llm_configured": True})()
    monkeypatch.setattr(config, "get_settings", lambda: fake)

    assert llm_is_configured() is True


# ---------------------------------------------------------------------------
# 出图请求（D5）：计划阶段就要说清"画哪两列"
# ---------------------------------------------------------------------------


def test_chart_is_attached_only_to_tools_that_return_rows(sales_profile: DatasetProfile) -> None:
    """只有"结果里有 rows"的四个工具配图；另外三个不配。

    这不是"顺手多给几个"：`describe_dataset` 的 `stats` 是"每列一行统计量"，
    画出来是 9 根含义不同的柱子；`missing_values` 的绝大多数列是 0，图上只有
    一两根柱子立着；`correlation` 只有 `pairs`（哪两列 + 一个系数），
    没有原始数据点。三种都不该出图。
    """
    plan = plan_with_rules("各地区的销售额、缺失值、趋势和关系都要看", sales_profile)
    by_tool = {task.tool_name: task for task in plan.tasks}

    for tool_name in ("group_aggregate", "top_n", "compare_groups", "time_series_summary"):
        task = by_tool.get(tool_name)
        if task is None:  # 这个问题没命中那条规则，跳过（另有专门的用例覆盖）
            continue
        assert task.chart is not None, f"{tool_name} 应该配图"

    for tool_name in ("describe_dataset", "missing_values", "correlation"):
        task = by_tool.get(tool_name)
        if task is None:
            continue
        assert task.chart is None, f"{tool_name} 不该配图"


def test_chart_fields_match_what_the_tool_actually_returns(
    sales_profile: DatasetProfile, sales_df: pd.DataFrame
) -> None:
    """**图里画的两列必须是工具结果里真实存在的列** —— 这条是出图正确性的核心。

    列名推导规则写在 `planner._chart_for` 的注释里（`{metric}_{agg}` 等），
    它和工具实现是两处独立的代码，很容易对不上。对不上的表现是：
    `charts.py` 走兜底路径，用别的列画图，而标题写着"销售额" ——
    图看着正常，画的却是另一列。

    所以这里**真的跑一遍工具**，拿它返回的 `columns` 来核对，而不是手抄一份
    预期的列名（手抄的清单会和工具实现一起腐烂）。
    """
    plan = plan_with_rules("各地区的销售额、最高的商品、趋势和比较都要看", sales_profile)

    checked = 0
    for task in plan.tasks:
        if task.chart is None:
            continue
        result = run_task(task, sales_df)
        assert result.ok, f"{task.tool_name} 应该执行成功：{result.error_message}"

        real_columns = set(result.data.get("columns") or [])
        rows = result.data.get("rows") or []
        if rows:
            real_columns |= set(rows[0].keys())

        assert task.chart.x_field in real_columns, (
            f"{task.tool_name} 的 x_field={task.chart.x_field!r} 不在工具返回的列里：{real_columns}"
        )
        assert task.chart.y_field in real_columns, (
            f"{task.tool_name} 的 y_field={task.chart.y_field!r} 不在工具返回的列里：{real_columns}"
        )
        checked += 1

    assert checked >= 3, f"这个问题应该至少命中 3 个出图任务，实际 {checked} 个"


def test_chart_titles_respect_the_contract_limit(sales_profile: DatasetProfile) -> None:
    """`ChartRequest.title` 的契约上限是 40 字 —— 超了 `TaskPlan` 会直接抛异常。

    规则生成的描述都很短，这条更像一道闸门：以后有人把描述写长（比如把整个问题
    抄进标题），这里会立刻红，而不是等用户提问时才炸。
    """
    for question in QUESTIONS:
        plan = plan_with_rules(question, sales_profile)
        for task in plan.tasks:
            if task.chart is None:
                continue
            assert task.chart.title, "标题不能是空的"
            assert len(task.chart.title) <= 40, f"标题太长：{task.chart.title!r}"


def test_chart_types_are_only_bar_or_line(sales_profile: DatasetProfile) -> None:
    """规则路径只出 bar 与 line —— 不出 pie/scatter。

    饼图在 6 个分组时还能看，20 个分组时是一圈细线；散点图需要两个数值列，
    而规则计划器产出的任务里没有"两个数值列"这种形状。契约允许四种，
    不代表每种都该被自动用上。
    """
    for question in QUESTIONS:
        plan = plan_with_rules(question, sales_profile)
        for task in plan.tasks:
            if task.chart is None:
                continue
            assert task.chart.chart_type in {"bar", "line"}, (
                f"{task.tool_name} 产出了 {task.chart.chart_type}"
            )


def test_trend_gets_a_line_chart_and_aggregates_get_bars(sales_profile: DatasetProfile) -> None:
    """图型要跟 `kind` 对上：趋势是折线，聚合/比较是柱状。

    折线图表达"随时间变化"，柱状图表达"类别之间的大小"。给趋势配柱状图不算错，
    但读者会下意识去比"哪根柱子高"，而趋势问题的答案是"在涨还是在跌"。
    """
    plan = plan_with_rules("各月份的销售额变化、各地区的销售额比较", sales_profile)
    by_tool = {task.tool_name: task for task in plan.tasks}

    trend = by_tool.get("time_series_summary")
    assert trend is not None and trend.chart is not None
    assert trend.chart.chart_type == "line"

    for tool_name in ("group_aggregate", "compare_groups"):
        task = by_tool.get(tool_name)
        if task is None:
            continue
        assert task.chart is not None
        assert task.chart.chart_type == "bar"


def test_top_n_uses_the_same_column_for_both_axes(sales_profile: DatasetProfile) -> None:
    """`top_n` 只返回一列（排序列），所以 x 和 y 都是它 —— x 轴用行号当标签。

    `top_n` 不传 `columns` 时只返回"分组列 + 排序列"，这里只有排序列，
    所以 x_field 和 y_field 必然是同一个名字（`charts.py` 据此改用行号）。
    """
    plan = plan_with_rules("找出销售额最高的十个商品", sales_profile)
    task = _first_task(plan, "top_n")

    assert task is not None and task.chart is not None
    assert task.chart.x_field == task.chart.y_field == "销售额"


def test_chart_requests_are_never_invented_for_another_dataset() -> None:
    """**反向证伪**：换一份列名完全不同的数据，不能产出写死的"地区/销售额"。

    这是最危险的一类 bug：在 `sales.csv` 上一切正常（因为列名刚好对得上），
    换个数据集就画错列 —— 或者更糟，任务失败。所以这里造一个三列小数据集
    （`aa`/`bb`/`cc`，与中文列名毫无关系），确认产出的字段名只可能来自它自己。

    注意 `y_field` 有两种来源，断言方式必须分开：
    - `top_n` 的 `y_field` 是**画像里的列**（就是 `by` 那一列）；
    - 聚合类的 `y_field` 是**工具现算出来的派生列**（`bb_sum`），画像里根本没有它 ——
      所以对它的断言是"真的跑一遍工具，看它返回的列里有没有这个名字"，
      而不是"它在画像里"。把后者写进断言，会在任何数据集上误报。
    """
    df = pd.DataFrame(
        {
            "aa": ["x", "y", "x", "y"],
            "bb": [1.0, 2.0, 3.0, 4.0],
            "cc": [10, 20, 30, 40],
        }
    )
    profile = build_profile(df, "ds-tiny")

    plan = plan_with_rules("各 aa 的 bb 汇总、bb 最高的前 2 行、bb 的趋势", profile)
    real_columns = {column.name for column in profile.columns}

    checked = 0
    for task in plan.tasks:
        if task.chart is None:
            continue

        # x_field 一定是画像里的列（它是分组维度，不是算出来的）
        assert task.chart.x_field in real_columns, f"x 字段是编的：{task.chart.x_field}"
        assert "地区" not in (task.chart.x_field, task.chart.y_field)
        assert "销售额" not in (task.chart.x_field, task.chart.y_field)

        # y_field 用工具的真实输出来核对（派生列只存在于工具结果里）
        result = run_task(task, df)
        assert result.ok, f"{task.tool_name} 应该执行成功：{result.error_message}"
        produced = set(result.data.get("columns") or [])
        rows = result.data.get("rows") or []
        if rows:
            produced |= set(rows[0].keys())
        assert task.chart.y_field in produced, (
            f"{task.tool_name} 的 y_field={task.chart.y_field!r} 不在工具返回的列里：{produced}"
        )
        checked += 1

    assert checked >= 2, f"这份数据应该至少命中 2 个出图任务，实际 {checked} 个"


def test_chart_requests_survive_a_round_trip_through_the_model_path() -> None:
    """模型路径也要配图：`_tasks_from_reply` 造任务时同样调 `_chart_for`。

    走模型时参数是模型给的，`_chart_for` 只认参数里的键 —— 所以这里直接调它，
    断言"模型给的合法参数"能产出正确的字段名。不 mock 网络：`_chart_for` 是
    纯函数，测它不需要模型。
    """
    from app.agents.planner import _chart_for

    chart = _chart_for(
        "group_aggregate",
        {"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
        "按地区分组汇总销售额",
    )

    assert chart is not None
    assert chart.x_field == "地区"
    assert chart.y_field == "销售额_sum"
    assert chart.chart_type == "bar"


def test_aggregate_suffix_follows_the_agg_the_model_asked_for() -> None:
    """**派生列名必须跟着 `agg` 走**，不能写死一个后缀。

    规则路径只会产出 `agg="mean"`（`compare_groups` 的默认值），所以
    "写死 `_mean`"这个 bug 在规则路径上**完全看不出来** —— 它只在模型
    自己指定了 `agg="sum"` 时才现形：工具返回 `销售额_sum`，图请求却写
    `销售额_mean`，于是 `charts.py` 走兜底路径拿最后一列画图，
    标题写着"销售额"而画的其实是"差值"那一列。

    这条直接调 `_chart_for` 并显式传 `agg="sum"`，就是为了盖住规则路径
    永远走不到的那个分支。
    """
    from app.agents.planner import _chart_for

    summed = _chart_for(
        "compare_groups",
        {"group_by": "地区", "metric": "销售额", "agg": "sum"},
        "按地区分组比较销售额",
    )
    default = _chart_for("compare_groups", {"group_by": "地区", "metric": "销售额"}, "比较")

    assert summed is not None and default is not None
    assert summed.y_field == "销售额_sum", "agg=sum 时派生列是 销售额_sum"
    assert default.y_field == "销售额_mean", "不传 agg 时 compare_groups 的默认值是 mean"

    # 时间序列与分组汇总同理，三个工具的命名规则是同一套（`{metric}_{agg}`）
    series = _chart_for(
        "time_series_summary", {"date_column": "日期", "metric": "销售额", "agg": "mean"}, "走势"
    )
    assert series is not None
    assert series.y_field == "销售额_mean"


def test_chart_for_returns_none_when_params_are_incomplete() -> None:
    """参数缺关键项 → 返回 `None`，不抛异常，**更不许编一个列名顶上**。

    计划阶段的任务本来就该"能执行就执行"：配不出图顶多少一张图，
    不该让整份计划失败（`TaskPlan` 一抛，用户连数字都拿不到）。

    但"不抛异常"不等于"随便给个字段名"：如果这里编一个列名，
    图会画出来、标题也对，画的内容却和标题无关 —— 那比没有图更糟。
    """
    from app.agents.planner import _chart_for

    assert _chart_for("group_aggregate", {}, "标题") is None
    assert _chart_for("compare_groups", {"group_by": "地区"}, "标题") is None
    assert _chart_for("time_series_summary", {}, "标题") is None
    assert _chart_for("top_n", {}, "标题") is None
    assert _chart_for("describe_dataset", {}, "标题") is None
    assert _chart_for("missing_values", {}, "标题") is None
    assert _chart_for("correlation", {}, "标题") is None


def test_long_titles_are_clipped_instead_of_breaking_the_plan() -> None:
    """标题超长 → 截断，而不是让 `ChartRequest` 抛 `ValidationError`。

    拿契约去惩罚用户（因为描述长了两个字，整份计划构造失败）代价完全不成比例。
    """
    from app.agents.planner import _chart_for

    chart = _chart_for("top_n", {"by": "销售额", "n": 10}, "很长的标题" * 20)

    assert chart is not None
    assert len(chart.title) <= 40
    assert chart.title.endswith("…")
