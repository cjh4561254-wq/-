"""守护测试：用户看到的那句话里不能出现内部字段名。

**背景（task-28 缺陷 A）**：`app/agents/validation.py` 的 `TEMPLATE_NOTICE` 曾经是

    本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总

这句话**直接上屏**（报告页与历史详情抽屉都渲染 `FinalReport.summary`）。
`llm_used` 是 API/DB 字段名，用户拿着它既不能行动、也不能据此判断数字可不可信；
判断依据是"这次到底用没用模型"这半句话本身。同文件的 `LLM_PLANNED_NOTICE`
一直是用户视角措辞（没有字段名），所以这曾经是同一文件里两套写法。

**为什么这条测试不能只断言常量本身**：`startswith(TEMPLATE_NOTICE)` 那种写法是
"拿常量和它自己比"，永远为真（`tests/test_orchestrator.py:188-192` 的作者注释
记了这件事）。这里做的两件事是不同的：

1. `test_report_summary_has_no_internal_field_names` 走**真实渲染路径** ——
   真的调 `build_report()` 生成一份报告，再检查它的 `summary`（用户实际看到的东西），
   而不是检查常量。将来若有人把字段名挪到函数里拼字符串，这条仍会红。
2. `test_template_notice_keeps_both_layers_of_information` 钉住**语义没被改弱** ——
   去掉字段名时最容易顺手把"未使用大模型"这半句也删了（那样用户就分不清
   "没用模型"和"用了模型但正文是模板写的"），这条会红。

`tests/test_validation.py:292` 那条"源码常量 == 测试副本"的等式是另一件事
（漂移探测），本文件不重复它。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.agents import validation
from app.agents.validation import (
    LLM_PLANNED_NOTICE,
    LLM_WROTE_NOTICE,
    build_report,
)
from app.schemas_workflow import WorkflowState

# 形如 `llm_used=false` / `llm_used=False` / `foo = true` 的内部字段名。
ASSIGNMENT_IN_TEXT = re.compile(r"\b[a-z_][a-z0-9_]*\s*=\s*(?:true|false|True|False)\b")

# 裸的蛇形字段名（例如"（llm_used）"）。`\w` 边界保证不会误伤
# `_summarize` 这类标识符出现在代码块里的正常情况。
BARE_FIELD_NAME = re.compile(r"(?<![`\w])llm_used(?![`\w])")


def _state() -> WorkflowState:
    """一个"什么都没配"的状态：走模板报告那条路（也就是 `TEMPLATE_NOTICE` 那条）。"""
    return WorkflowState(
        workflow_id="a" * 32,
        dataset_id="0123456789abcdef0123456789abcdef",
        question="分析不同地区的销售额",
        created_at=datetime.now(timezone.utc),
    )


def test_report_summary_has_no_internal_field_names() -> None:
    """真实报告的 `summary` 里不许出现 `字段名=值` 这种内部写法。"""
    report = build_report(_state())

    hits = ASSIGNMENT_IN_TEXT.findall(report.summary)
    assert not hits, f"报告正文里出现了内部字段名赋值：{hits}\n首段：{report.summary.splitlines()[0]}"


def test_report_summary_has_no_bare_llm_used() -> None:
    """连裸的 `llm_used` 也不许出现 —— 上屏文本里它没有读者。"""
    report = build_report(_state())

    assert not BARE_FIELD_NAME.search(report.summary), (
        f"报告正文里出现了 `llm_used`：{report.summary.splitlines()[0]}"
    )


def test_template_notice_keeps_both_layers_of_information() -> None:
    """去掉字段名时**不许**顺手把信息量也去掉（这是本任务最容易被改坏的地方）。

    两层必须是字面的：
    - ① "未使用大模型" —— 用户据此知道这次没有模型参与；
    - ② "工具结果的直接汇总" —— 用户据此知道数字的来源是工具，不是模型生成的。
    含糊化（如"部分环节未使用大模型"）会让 ① 失真，这条会红。
    """
    notice = validation.TEMPLATE_NOTICE

    assert "未使用大模型" in notice, f"丢了「未使用大模型」这层：{notice!r}"
    assert "工具结果的直接汇总" in notice, f"丢了「工具结果的直接汇总」这层：{notice!r}"
    # 反向：不许出现"部分"/"某些"这类把 ① 打成半真的限定词。
    assert "部分" not in notice and "某些" not in notice, f"被改含糊了：{notice!r}"


def test_all_three_honesty_notices_are_user_facing() -> None:
    """三个诚实标记是同一套口径，任何一个带 `字段名=` 都是不一致。

    为什么连 `LLM_PLANNED_NOTICE` / `LLM_WROTE_NOTICE` 一起断言：当初这处缺陷
    能留下来，正因为只有**一个**常量带了字段名，别的没带 —— 单看那一个像"故意的"，
    放在一起才看得出是两套写法。这条把三者绑在同一个判据上，防止再漂回去。
    """
    for name, notice in (
        ("TEMPLATE_NOTICE", validation.TEMPLATE_NOTICE),
        ("LLM_PLANNED_NOTICE", LLM_PLANNED_NOTICE),
        ("LLM_WROTE_NOTICE", LLM_WROTE_NOTICE),
    ):
        assert not ASSIGNMENT_IN_TEXT.search(notice), f"{name} 里有内部字段名：{notice!r}"
        assert not BARE_FIELD_NAME.search(notice), f"{name} 里有 `llm_used`：{notice!r}"


def test_three_notices_stay_mutually_distinct() -> None:
    """三句话必须互不相同（`tests/test_validation.py` 已有同义断言，这里再钉一次端到端）。

    改文案最省事的做法是复制粘贴 —— 一旦两句相等，`llm_used=True` 的报告就会
    一边说"由大模型参与规划"、一边说"未使用大模型"。
    """
    notices = {
        validation.TEMPLATE_NOTICE,
        LLM_PLANNED_NOTICE,
        LLM_WROTE_NOTICE,
    }

    assert len(notices) == 3, f"三句诚实标记出现了重复：{notices}"


def test_summary_first_paragraph_is_exactly_the_notice_when_no_model_is_used() -> None:
    """首段必须**以**那句诚实标记开头（这是上屏的第一行，位置不能漂）。"""
    report = build_report(_state())

    assert report.summary.startswith(validation.TEMPLATE_NOTICE)
