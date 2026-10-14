"""`llm_used` 与诚实首段的测试：**把 D4 QA 发现的两个零覆盖问题钉住**。

D4 的 QA 复验（基线 `d4fd727`）查出两条重要问题，共同点是
**"数字没错，话是假的"**，而且**零测试覆盖** —— QA 把实现改成正确写法时
`490 passed` 一条都没红。这个文件就是那两条的护栏。

## 问题 1：报告把"最弱的一对"说成"最强的一对"

`app/tools/compare.py:76-85` 的 `pairs` 是**按列序**构造的（上三角遍历），
没有任何排序。`validation.py` 原来直接取 `pairs[0]`，于是在 sales.csv 上
报告会写「相关性最强的一对是 销量 与 单价，相关系数 -0.05」——
而真正 |r| 最大的是 销售额/利润 **0.9872**。

QA 的变异证据：把 `pairs[0]` 改成 `max(pairs, key=|r|)`（即改成正确写法）
→ `490 passed`，零红。所以这条断言不是锦上添花，它是这句话唯一的守卫。

## 问题 2：模型规划了，报告却说"未使用大模型"

D4 的 `validation.py:95` 把 `llm_used` 硬编码成 `False`，理由是
"D4 只有模板路径"。但真 key 接入后模型确实参与了**规划**
（`plan.source == "llm"`），此时报告首段、接口 7 的 `llm_used`、前端横幅
三处都显示"未使用大模型"。`app/schemas_workflow.py:210` 的 docstring 把
`llm_used` 定义为"这次到底有没有用模型"的**诚实标记** —— 两头必须对上。

口径（`app/agents/validation.py:_llm_used`）：
    rule_fallback → False ；llm → True。只认 `plan.source`，不猜。

## 一个刻意的设计：这里不 import 实现里的常量

`TEMPLATE_NOTICE` / `LLM_PLANNED_NOTICE` 是**逐字**断言的目标，
从被断言的那一边 import 常量来比自己，是一句永远为真的断言
（`tests/test_orchestrator.py:188-192` 记着这个教训，那里就是这么改的）。
"""

from __future__ import annotations

from typing import Any

import pytest
from datetime import datetime, timezone

from app.agents.validation import _summarize_correlation, build_report
from app.data.loader import load_dataframe
from app.data.store import DatasetStore
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    AnalysisTask,
    FinalReport,
    TaskPlan,
    TaskStatus,
    WorkflowState,
)
from app import config

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"


def _now() -> datetime:
    """统一用 aware 的 UTC 时间 —— 与 orchestrator 的口径一致。

    写成函数而不是模块常量：模块级 `datetime.now()` 会在 import 那一刻冻结，
    多个用例共用同一个 `created_at` 时看不出"时间有没有被真的写过"。
    """
    return datetime.now(timezone.utc)# 逐字写死。理由见模块 docstring：不许从实现里 import 来比自己。
TEMPLATE_NOTICE = "本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总"
LLM_PLANNED_NOTICE = (
    "本次由大模型参与规划（选择了分析工具与参数），"
    "但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总"
)


@pytest.fixture(scope="module")
def sales_df():
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture
def basket(sales_df):
    """独立的 DatasetStore（不污染进程内单例），返回 `(store, dataset_id)`。"""
    fake_store = DatasetStore()
    dataset_id = "0123456789abcdef0123456789abcdef"
    fake_store.register(dataset_id, sales_df, "sales.csv", "csv", 1024, "sha-test")
    return fake_store, dataset_id


def _state_with_plan(dataset_id: str, source: str, **overrides: Any) -> WorkflowState:
    """造一个"计划已生成、报告还没写"的状态。

    `plan.source` 是这个文件大部分用例的自变量 —— 两句话、一个 `llm_used`，
    全都由它决定。
    """
    fields: dict[str, Any] = {
        "workflow_id": "a" * 32,
        "dataset_id": dataset_id,
        "question": "分析不同地区的销售额",
        "created_at": _now(),
        "plan": TaskPlan(
            plan_id="p1",
            question="分析不同地区的销售额",
            dataset_id=dataset_id,
            tasks=[
                AnalysisTask(
                    task_id="T1",
                    order=1,
                    kind="aggregate",
                    tool_name="group_aggregate",
                    params={"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
                    description="按地区汇总销售额",
                    status=TaskStatus.SUCCESS,
                )
            ],
            source=source,
            created_at=_now(),
        ),
    }
    state = WorkflowState(**fields)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ---------------------------------------------------------------------------
# 1. 问题 1：correlation 必须挑"绝对值最大"的那一对
# ---------------------------------------------------------------------------


def _correlation_result(pairs: list[dict[str, Any]]) -> ToolResult:
    """造一个 `correlation` 工具的成功结果。"""
    return ToolResult(
        task_id="T2",
        tool_name="correlation",
        ok=True,
        data={"columns": ["销量", "单价", "销售额", "利润"], "pairs": pairs},
        columns=["x", "y", "r"],
        row_count=len(pairs),
        elapsed_ms=1,
        created_at=_now(),
    )


def test_correlation_picks_the_largest_absolute_r_not_the_first_pair():
    """`pairs` 按列序排列，报告挑的必须是 |r| 最大的一对。

    这是 D4 QA 重要问题 1 的守卫：QA 把 `pairs[0]` 改成正确写法时
    一条测试都没红，说明这句话当时**没有任何断言钉住**。
    这里故意把"最强的一对"放在**第二个**位置，让"取第一个"的实现当场失败。
    """
    pairs = [
        {"x": "销量", "y": "单价", "r": -0.0457},
        {"x": "销售额", "y": "利润", "r": 0.9872},
        {"x": "销量", "y": "利润", "r": 0.5123},
    ]

    sentence = _summarize_correlation({"pairs": pairs})

    assert "销售额" in sentence and "利润" in sentence
    assert "销量" not in sentence, "最弱的一对不该出现在这句话里"
    assert "0.99" in sentence, "报告里必须出现真实的最大 |r|（0.9872 四舍五入到两位）"


def test_correlation_handles_all_negative_r_by_magnitude():
    """全负相关时按**绝对值**挑：-0.9 比 -0.1 更强。

    这一条防的是另一种错法 —— `max(pairs, key=lambda p: p["r"])`：
    在所有 r 都是负数时会返回最接近 0 的那个，同样是把最弱的说成最强的。
    """
    pairs = [
        {"x": "a", "y": "b", "r": -0.11},
        {"x": "c", "y": "d", "r": -0.94},
    ]

    sentence = _summarize_correlation({"pairs": pairs})

    assert "c" in sentence and "d" in sentence
    assert "a" not in sentence and "b" not in sentence


def test_correlation_ignores_pairs_without_a_number():
    """`r` 是 None 的列对（某一列没有波动时）必须被排除，不能拿来比较。

    这也是一条有牙的断言：实现里若少一层过滤，`abs(None)` 会 TypeError，
    而 `_summarize_correlation` 是被 `build_report` 的 try/except 兜住的 ——
    异常会被吞掉、报告退化成"没有算出可用的相关系数"，用户看不出这里出错。
    """
    pairs = [
        {"x": "常数列", "y": "销量", "r": None},
        {"x": "销售额", "y": "利润", "r": 0.62},
    ]

    sentence = _summarize_correlation({"pairs": pairs})

    assert "销售额" in sentence and "利润" in sentence
    assert "常数列" not in sentence


def test_correlation_sentence_reports_the_pair_count_it_compared():
    """"共比较了 N 对"里的 N 必须是真的参与比较的对数。

    它把"我查过几对"变成一个可核对的数字 —— 用户看到"共比较了 2 对"
    再看到表格里有 4 行，就知道没有漏。
    """
    pairs = [
        {"x": "销量", "y": "单价", "r": -0.0457},
        {"x": "销售额", "y": "利润", "r": 0.9872},
    ]

    sentence = _summarize_correlation({"pairs": pairs})

    assert "共比较了 2 对" in sentence


# ---------------------------------------------------------------------------
# 2. 问题 2：llm_used 与首段必须按 plan.source 走
# ---------------------------------------------------------------------------


def test_rule_fallback_plan_reports_no_model_was_used(basket):
    """规则兜底 → `llm_used=False` + 逐字的"未使用大模型"首段。"""
    _, dataset_id = basket
    state = _state_with_plan(dataset_id, "rule_fallback")

    report = build_report(state)

    assert report.llm_used is False
    assert report.summary.startswith(TEMPLATE_NOTICE)
    assert LLM_PLANNED_NOTICE not in report.summary


def test_llm_plan_reports_the_model_did_take_part(basket):
    """模型规划 → `llm_used=True` + 换成"模型参与了规划"的首段。

    这是 D4 QA 重要问题 2 的守卫。D4 时 `llm_used` 硬编码 `False`，
    于是真 key 规划成功后，报告首段、接口 7 的 `llm_used`、前端横幅
    三处一致地说"未使用大模型" —— 而计划明明是模型给的。
    数字一个没错，错的是"哪一步用了模型"这句陈述。
    """
    _, dataset_id = basket
    state = _state_with_plan(dataset_id, "llm")

    report = build_report(state)

    assert report.llm_used is True
    assert report.summary.startswith(LLM_PLANNED_NOTICE)
    # 不能留半句旧的假陈述
    assert TEMPLATE_NOTICE not in report.summary


def test_llm_used_is_false_when_the_plan_is_missing(basket):
    """连计划都没有（比规划更早的地方就失败了）→ `llm_used=False`。

    这条口径必须钉住，否则"没有计划"会被算成"用了模型"——
    而这正是前端横幅亮起"AI 模式"的依据。
    """
    _, dataset_id = basket
    state = _state_with_plan(dataset_id, "llm", plan=None)

    report = build_report(state)

    assert report.llm_used is False
    assert report.summary.startswith(TEMPLATE_NOTICE)


def test_minimal_report_keeps_the_same_honesty_marker(basket):
    """兜底报告（模板自己有 bug 时那条路）也要走同一套口径。

    D4 的第二处硬编码就在 `_minimal_report` 里。绕过 `_build` 直取兜底
    很麻烦，所以这里反过来钉行为：**任何**实现下，兜底报告的首段都必须
    与"这次到底有没有用模型"一致。改坏 `_minimal_report` 会让这条红。
    """
    _, dataset_id = basket
    # LLM 计划 + 一个会让 _build 崩的 results（把 results 塞成非 ToolResult 对象）
    state = _state_with_plan(dataset_id, "llm")
    state.results = [object()]

    report = build_report(state)

    assert isinstance(report, FinalReport)
    assert report.llm_used is True
    assert report.summary.startswith(LLM_PLANNED_NOTICE)


def test_both_notices_are_word_for_word_the_ones_the_frontend_documents(basket):
    """两句话必须逐字匹配，且**互不相同**。

    "互不相同"不是废话：如果有人把 `LLM_PLANNED_NOTICE` 抄成
    `TEMPLATE_NOTICE` 的别名，`llm_used=True` 的报告就会一边说
    "由大模型参与规划"、一边说"未使用大模型"。两个常量相等时这条会红。
    """
    from app.agents import validation as validation_module

    assert validation_module.TEMPLATE_NOTICE == TEMPLATE_NOTICE
    assert validation_module.LLM_PLANNED_NOTICE == LLM_PLANNED_NOTICE

    _, dataset_id = basket
    llm_report = build_report(_state_with_plan(dataset_id, "llm"))
    rule_report = build_report(_state_with_plan(dataset_id, "rule_fallback"))

    assert llm_report.summary != rule_report.summary
