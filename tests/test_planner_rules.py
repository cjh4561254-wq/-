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
from app.agents.planner import llm_is_configured, plan_with_rules
from app.data.loader import load_dataframe
from app.data.profiler import build_profile
from app.schemas_dataset import DatasetProfile
from app.schemas_workflow import TaskPlan
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
