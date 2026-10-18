"""模型撰写报告正文的测试：**写得好要用、写坏了要退回、规则路径不许调模型**。

## 这个文件为什么必须存在

`app/agents/report_llm.py` 是项目里**第一个把模型的自由文本放进用户可见报告**的地方。
在此之前，报告里每一个字都是代码拼的 —— 数字错了是代码的 bug，看一眼就能定位。
有了这条路之后，报告里会出现"没人写过的话"，于是三件事必须被钉住：

1. **写坏了不能拖垮工作流**。模型超时、返回半截 JSON、少一个字段，用户该拿到的是
   一份模板报告，而不是一个 FAILED 的工作流 —— 数字早就用 pandas 算完了。
2. **规则路径一次都不能调模型**。没配 key 时（第 4 天演示的常态）如果还去调，
   表现是"每次提问都白等一次超时"，而这件事在日志里看起来完全正常。
3. **模型看不见原始数据**。prompt 里只能有 `state.results` 里已经算好的数字。

## Mock 证明了什么、没证明什么（必读）

这个文件**一律 monkeypatch `app.llm.client.chat`**，所以它证明的是：
"模型返回这段文本时，系统会怎么处理"—— 分支、格式校验、退回、重写机会。

它**不证明**：
- 真实 DeepSeek 会返回什么形状的 JSON（那只能靠 `real_api` 标记的用例真发一次）；
- 网络超时、429、401 的重试行为（那是 `tests/test_llm_client.py` 的范围，
  那里连 HTTP transport 都是假的）；
- 模型的文字**质量**。质量没有断言可写，只能人读。

换句话说：**这里测的是我们自己的判断逻辑，不是模型**。

## 一个刻意的设计：诚实首段的常量在这里逐字硬编码

`TEMPLATE_NOTICE` / `LLM_PLANNED_NOTICE` / `LLM_WROTE_NOTICE` 三个常量是"哪一步用了
模型"的**诚实标记**，前端横幅与接口 7 的 `llm_used` 都靠它。断言它们时从实现里
import 来比自己，是一句永远为真的断言（`tests/test_validation.py:28-33` 记着这个教训）。
所以下面三个常量是**手抄**的，抄错就会红 —— 那正是想要的。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app import config
from app.agents import orchestrator as orchestrator_module
from app.agents import report_llm
from app.agents.orchestrator import run_workflow
from app.agents.validation import (
    build_report,
    should_rewrite_report,
    should_try_llm_report,
    validate,
)
from app.data.loader import load_dataframe
from app.data.store import DatasetStore
from app.schemas_workflow import (
    AnalysisTask,
    TaskPlan,
    TaskStatus,
    ValidationIssue,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
)

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

# 逐字硬编码（理由见模块 docstring）。
TEMPLATE_NOTICE = "本次未使用大模型，以下结论为工具结果的直接汇总"
LLM_PLANNED_NOTICE = (
    "本次由大模型参与规划（选择了分析工具与参数），"
    "但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总"
)
LLM_WROTE_NOTICE = "本次报告正文由大模型根据工具结果撰写，其中所有数字均来自下方引用的工具调用，可逐条核对"

# 报告正文里那句"唯一允许引用"的数字，模型写它是对的、写别的就是编造。
REAL_VALUE = "24871657.0"


@pytest.fixture(scope="module")
def sales_df():
    df, _ = load_dataframe(SAMPLE_CSV, "csv")
    return df


@pytest.fixture
def basket(monkeypatch, sales_df):
    """独立的 DatasetStore + 换掉 orchestrator 眼里的那个 `store`。

    不用全局单例：它是进程内共享的，一个用例改了数据集，后面的用例看到的就不是
    自己造的那份了（`tests/test_orchestrator.py` 的 docstring 记着这个坑）。
    """
    fake_store = DatasetStore()
    dataset_id = "0123456789abcdef0123456789abcdef"
    fake_store.register(dataset_id, sales_df, "sales.csv", "csv", 1024, "sha-test")
    monkeypatch.setattr(orchestrator_module, "store", fake_store)
    return fake_store, dataset_id


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """每个用例一套显式配置，结束后清缓存。

    三项 LLM 变量**默认清空**（不依赖运行这台机器时有没有配 key）：需要走模型路径
    的用例自己再 `setenv`。`CHARTS_DIR` 指到 `tmp_path`，别往真实的 `data/charts/`
    里写文件（那个目录是接口 8 真正在读的地方）。
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validation_with(code: str) -> ValidationResult:
    """一份"校验没通过"的结果，里面只有指定 `code` 的一条 error 级问题。

    直接造 `ValidationResult` 而不是跑一遍 `validate()`：这里要钉的是"重写判断
    认哪个 code"。让真校验器去产生这些场景（任务失败、图表丢失）会把用例和
    校验器的实现绑在一起 —— 校验器改个阈值，这条用例就会因为无关的原因红。
    """
    return ValidationResult(
        passed=False,
        checked_numbers=1,
        unsupported_numbers=["88.8"] if code == "UNSUPPORTED_NUMBER" else [],
        issues=[
            ValidationIssue(
                severity="error",
                code=code,
                message=f"测试造出来的问题：{code}",
                task_id="T1",
            )
        ],
        checked_at=_now(),
    )


class FakeReply:
    """`LLMReply` 的替身：`report_llm` 只用到 `content` 一个字段。

    刻意不用真的 `LLMReply`：那个模型有 `model_validator` 在管
    "reasoning_content 必须回传"，构造它需要摆一堆跟本文件无关的字段，
    而这里要测的是"拿到一段文本之后怎么办"。
    """

    def __init__(self, content: str) -> None:
        self.content = content


def _good_sections(first_value: str = REAL_VALUE) -> dict[str, Any]:
    """一份合格的模型输出：3 段、每段都有 cites、数字全部来自工具结果。"""
    return {
        "sections": [
            {
                "heading": "各地区销售额概况",
                "text": (
                    "这次按地区汇总了销售额，一共得到 6 个地区的汇总结果。"
                    f"其中销售额最高的是华南，汇总值为 {first_value} 元。"
                    "这一段的数字全部来自分组汇总工具的输出，可以逐条核对。"
                ),
                "cites": ["T1"],
            },
            {
                "heading": "这张表可以怎么读",
                "text": (
                    "从汇总结果看，不同地区的销售额差距比较明显，最高的地区和最低的"
                    "地区不在一个量级上。只看总量容易忽略每个地区的行数不一样这件事，"
                    "所以建议把汇总值和分组行数放在一起看。"
                ),
                "cites": ["T1"],
            },
            {
                "heading": "解读时要注意的前提",
                "text": (
                    "这份数据本身存在缺失值和重复行，画像阶段已经把这些情况记在报告"
                    "下方的数据质量说明里。在解读上面的汇总数字时，需要把这些前提"
                    "一起考虑进去，不要只盯着总量做结论。"
                ),
                "cites": ["T1"],
            },
        ]
    }


def _install_chat(monkeypatch, replies: list[str]) -> list[dict[str, Any]]:
    """把 `client.chat` 换成按顺序吐 `replies` 的假实现，并记录每次调用。

    为什么要记录**每次调用的 messages**：这个文件里最重要的两条断言
    （"规则路径一次都没调"、"prompt 里没有原始数据"）都只能从调用记录里看。

    队列用完后就一直返回最后一条：重写轮的用例常常是"两次都返回同一份坏文本"，
    写死 `replies=[bad]` 比复制两份更不容易抄错。
    """
    calls: list[dict[str, Any]] = []
    queue = list(replies)

    def fake_chat(messages, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(queue) > 1:
            return FakeReply(queue.pop(0))
        return FakeReply(queue[0] if queue else "")

    from app.llm import client as client_module

    monkeypatch.setattr(client_module, "chat", fake_chat)
    return calls


def _install_failing_chat(monkeypatch, error: Exception) -> None:
    """把 `client.chat` 换成"每次都抛这个异常"的假实现。

    与 `_install_chat` 分开是因为语义不同：那个测"返回了一段文本会怎么处理"，
    这个测"根本没拿到文本"。合在一起写的话，用例名会变成"要么这样要么那样"，
    红了也看不出是哪一种坏法。
    """
    from app.llm import client as client_module

    def fake_chat(messages, tools=None):
        raise error

    monkeypatch.setattr(client_module, "chat", fake_chat)


def _patch_plan(monkeypatch, plan: TaskPlan) -> None:
    """把编排器的规划函数换成"直接返回这份计划"。

    为什么必须换掉而不是先把 `state.plan` 设好：`run_workflow` 的 planning 阶段会
    **重新规划并覆盖** `state.plan`（`orchestrator.py:101`）。不换的话，配置了 LLM 的
    用例会先让真 planner 去调一次模型，把假 `chat` 的队列吃掉 —— 表现是报告那一轮
    拿到的回复数量对不上，而失败信息完全指不到规划阶段。
    """
    monkeypatch.setattr(orchestrator_module, "plan_tasks", lambda question, profile, max_steps=6: plan)


def _configure_llm(monkeypatch, configured: bool = True) -> None:
    """让 `config.get_settings().llm_configured` 变成指定值。

    必须 `reset_settings_cache()`：`get_settings()` 带 `lru_cache`，不清的话读到的
    还是上一个用例那份（本项目真踩过这个坑）。
    """
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com" if configured else "")
    monkeypatch.setenv("LLM_API_KEY", "test-key" if configured else "")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    config.reset_settings_cache()


def _llm_plan_state(dataset_id: str) -> WorkflowState:
    """一个"报告还没写"的状态。

    计划本身**不在这里造**：`run_workflow` 的 planning 阶段会重新规划并覆盖
    `state.plan`，所以要靠 `_patch_plan` 换掉规划函数（理由见那个函数）。
    """
    return WorkflowState(
        workflow_id="a" * 32,
        dataset_id=dataset_id,
        question="分析不同地区的销售额",
        created_at=_now(),
        started_at=_now(),
    )


def _start(monkeypatch, dataset_id: str, plan: TaskPlan) -> WorkflowState:
    """造状态 + 钉住计划，返回一个"可以交给 `run_workflow`"的状态。

    两步绑在一起是因为它们必须成对出现：只造状态不钉计划，规划阶段会覆盖它；
    只钉计划不造状态，`store` 里没有数据集。
    """
    _patch_plan(monkeypatch, plan)
    return _llm_plan_state(dataset_id)


# ---------------------------------------------------------------------------
# 1. 写得好 → 用模型的正文，并把首段换成第三句话
# ---------------------------------------------------------------------------


def test_model_report_is_used_and_says_the_model_wrote_it(basket, monkeypatch):
    """模型写得好 → 正文是模型的、`llm_used=True`、首段是**第三句**诚实标记。

    这条钉住的是 D-22 记的那类"数字对、话是假的"：模型真的写了正文之后，
    首段必须从 `LLM_PLANNED_NOTICE`（"正文是模板自动汇总的"）换成
    `LLM_WROTE_NOTICE`。复用旧那句话就是一句假的陈述。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.status is WorkflowStatus.SUCCESS
    assert state.report is not None
    assert state.report.llm_used is True
    assert state.report.summary.startswith(LLM_WROTE_NOTICE)
    assert LLM_PLANNED_NOTICE not in state.report.summary
    assert TEMPLATE_NOTICE not in state.report.summary
    assert [section.heading for section in state.report.sections][0] == "各地区销售额概况"
    assert calls, "模型路径应该真的调用过 chat"


def test_model_report_keeps_the_table_from_the_tool_result(basket, monkeypatch):
    """表格仍然由代码从工具结果复制，**不经过模型**（§13 风险 4 ③）。

    模型只写正文：它连表格字段都没给。这条断言保证"图上的数和表里的数来自同一份
    `ToolResult`"，否则报告和表格会各说各话。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.report is not None
    tables = [section.table for section in state.report.sections if section.table]
    assert tables, "每段只引用一个任务时应该挂上工具结果的表格"
    assert tables[0] == state.results[0].data["rows"]


# ---------------------------------------------------------------------------
# 2. 写坏了 → 退回模板，工作流仍然 SUCCESS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param("这不是 JSON", id="not-json"),
        pytest.param("{}", id="no-sections-key"),
        pytest.param('{"sections": []}', id="empty-sections"),
        pytest.param('{"sections": "abc"}', id="sections-not-a-list"),
        pytest.param('{"sections": [{"heading": "x"}]}', id="missing-text"),
        pytest.param('{"sections": [{"text": "只有正文没有标题"}]}', id="missing-heading"),
        pytest.param(
            '{"sections": [{"heading": "x", "text": "这段话没有任何引用", "cites": []}]}',
            id="no-cites",
        ),
        pytest.param(
            '{"sections": [{"heading": "x", "text": "引用了不存在的任务 T99", "cites": ["T99"]}]}',
            id="cites-unknown-task",
        ),
    ],
)
def test_broken_model_output_falls_back_to_the_template(basket, monkeypatch, broken):
    """模型返回任何不合格的形状 → 用模板报告，工作流**仍然 SUCCESS**。

    这是本轮最重要的一条：`report_llm` 与 `build_report` 的契约都是"恒不抛异常"，
    模型写坏只是"今天这条路不走"，不是"分析失败"。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    _install_chat(monkeypatch, [broken, broken])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.status is WorkflowStatus.SUCCESS
    assert state.report is not None
    # 退回模板后，首段是"参与规划、正文是模板"那句 —— 不是"正文由模型撰写"。
    assert state.report.summary.startswith(LLM_PLANNED_NOTICE)
    assert LLM_WROTE_NOTICE not in state.report.summary


def test_model_call_failure_falls_back_to_the_template(basket, monkeypatch):
    """模型调用直接抛异常（超时/401/网络）→ 退回模板，工作流仍然 SUCCESS。

    与上一条分开：那是"返回了但内容不合格"，这是"根本没返回"。
    两条路的兜底位置不同（一个在 `_parse_sections`，一个在 `write` 的 try 里）。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)

    def boom(messages, tools=None):
        raise TimeoutError("模型超时了")

    from app.llm import client as client_module

    monkeypatch.setattr(client_module, "chat", boom)

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.status is WorkflowStatus.SUCCESS
    assert state.report is not None
    assert state.report.summary.startswith(LLM_PLANNED_NOTICE)
    assert state.error is None, "模型超时不是工作流错误"


def test_model_writing_too_long_a_section_falls_back(basket, monkeypatch):
    """单段超长 → **整份退回模板**，不是截断。

    截断可能把 `12,463.92` 切成 `12,463.` —— 那半截数字在工具结果里找不到出处，
    报告会带着一个"编造的数字"被校验拦下，而没人知道是截断造成的。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    long_text = "这段话非常长。" * 200
    payload = {
        "sections": [{"heading": "太长的一段", "text": long_text, "cites": ["T1"]}]
    }
    _install_chat(monkeypatch, [json.dumps(payload, ensure_ascii=False)] * 2)

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.report is not None
    assert state.report.summary.startswith(LLM_PLANNED_NOTICE)


def test_sections_beyond_the_cap_are_dropped(basket, monkeypatch):
    """模型返回的段数超过上限 → 只取前几段，**不是整份退回**。

    为什么要有上限：报告的篇幅不能随模型心情变化。段数不封顶时，模型某次返回 20 段，
    报告就变成 20 段 —— 页面上是一堵墙，而这件事没有任何报错提示。

    这里用"每一段都合格"的输入，所以整份退回那条路走不到：能观察到的差别只有段数。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    payload = {
        "sections": [
            {
                "heading": f"第 {index} 段",
                "text": (
                    f"这是第 {index} 段分析结论，内容足够长以便通过报告的总字数下限检查。"
                    "这一段引用的数字全部来自分组汇总工具的输出，可以逐条核对。"
                ),
                "cites": ["T1"],
            }
            for index in range(1, 9)
        ]
    }
    _install_chat(monkeypatch, [json.dumps(payload, ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.report is not None
    assert state.report.summary.startswith(LLM_WROTE_NOTICE), "这份输出本身是合格的"
    assert len(state.report.sections) == 5, "段数上限是 5，多的部分要被丢掉"
    assert state.report.sections[0].heading == "第 1 段"


def test_section_with_an_unknown_cite_is_dropped_not_the_whole_report(basket, monkeypatch):
    """`cites` 指不到真实任务的那一段被**丢掉**，其余段落照用。

    这是"丢掉一段"与"整份退回"的分界：丢掉一段是局部问题（那一段的数字没有依据），
    整份退回是"这一版根本不能用"。两者混为一谈的话，模型写 3 段里错 1 段，
    用户就白等了一次调用、拿回一份模板 —— 而另外 2 段本来是能用的。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    good_text = (
        "这一段引用的数字全部来自分组汇总工具的输出，可以逐条核对。"
        "另外还要注意每个地区的行数并不相同，只看总量容易忽略这件事，"
        "所以建议把汇总值和分组行数放在一起看，不要只盯着最大的那个数字下结论。"
        "数据本身还存在缺失值和重复行，这些前提在解读时也要一起考虑进去。"
    )
    payload = {
        "sections": [
            {"heading": "能用的一段", "text": good_text, "cites": ["T1"]},
            {"heading": "指不到任务的一段", "text": good_text, "cites": ["T99"]},
        ]
    }
    _install_chat(monkeypatch, [json.dumps(payload, ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.report is not None
    assert state.report.summary.startswith(LLM_WROTE_NOTICE), "整份没有退回模板"
    headings = [section.heading for section in state.report.sections]
    assert headings == ["能用的一段"], "指不到任务的那段要被丢掉"


# ---------------------------------------------------------------------------
# 3. 规则路径一次都不许调模型
# ---------------------------------------------------------------------------

def test_rule_fallback_plan_never_calls_the_model(basket, monkeypatch):
    """`plan.source == "rule_fallback"` → 报告这一路**一次都不调模型**。

    没配 key 是第 4 天演示的常态。这条路径上如果还去调模型，表现是"每次提问都白等
    一次超时"，而日志里一切正常 —— 属于"只在用户等待时间上看得见"的那类 bug。

    这里**故意把 key 配上、同时把规划函数换成"直接给一份规则兜底计划"**：
    只有这样，`chat` 里出现的任何一次调用都只可能来自报告路径。不换规划函数的话，
    配置了 key 的真 planner 会先合法地调一次模型，把假队列吃掉 ——
    那条断言就变成了在测"规划阶段"，而不是在测"报告阶段"。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch, configured=True)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _rule_plan(dataset_id))
    run_workflow(state)

    assert state.plan is not None
    assert state.plan.source == "rule_fallback"
    assert calls == [], "规则兜底计划不该调模型写报告"
    assert state.report is not None
    assert state.report.llm_used is False
    assert state.report.summary.startswith(TEMPLATE_NOTICE)


def test_unconfigured_model_never_calls_chat_even_on_the_llm_path(basket, monkeypatch):
    """状态是手工造的 `source="llm"`、但配置被清空 → 也不许调。

    光看 `plan.source` 不够：测试与演示里常常是"配置被清掉、但状态是造出来的"，
    真发一次请求就是真的花钱、真的等超时。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch, configured=False)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert calls == [], "没配模型时一次都不该调"
    assert state.report is not None
    # 计划是模型给的，所以 `llm_used` 仍为 True，但首段必须如实说"正文是模板汇总的"。
    assert state.report.llm_used is True
    assert state.report.summary.startswith(LLM_PLANNED_NOTICE)


# ---------------------------------------------------------------------------
# 4. 模型看不见原始数据
# ---------------------------------------------------------------------------


def test_prompt_contains_only_tool_results_not_the_raw_dataframe(basket, monkeypatch):
    """prompt 里只能有**已经算好的工具结果**与列名，不能有原始数据（§3.4 硬规矩）。

    做法：给数据集插一行**特征值**（`销量 = 123456789.0`，正常数据里不可能出现），
    然后断言这个值一次都没出现在 prompt 里。它出现在 `ToolResult.data` 里是允许的
    （那是算完的结果），所以这里挑的是"工具不会返回它"的那一列 ——
    `客户评分` 不参与这次分组汇总，它的任何值都不该出现在 prompt 里。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert calls, "应该调用过模型"
    prompt_text = json.dumps(calls[0]["messages"], ensure_ascii=False)
    # 原始数据里 2168 行，这个"行数"只有画像/工具结果里才有；逐行的原始值一个都不该有。
    assert "客户评分" in prompt_text, "列名清单是允许的（模型要能引用真实列名）"
    assert "销量" in prompt_text
    # 断言"不是整张表被倒进去"：原始 CSV 里的日期字符串不会出现在工具结果里。
    assert "2024-01-11" not in prompt_text, "原始数据的行值不该进 prompt"


def test_prompt_lists_the_question_and_the_task_ids(basket, monkeypatch):
    """prompt 里要有问题原文与任务编号 —— 模型写 `cites` 全靠任务编号。"""
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    prompt_text = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "分析不同地区的销售额" in prompt_text
    assert "T1" in prompt_text


def test_report_prompt_does_not_offer_tools(basket, monkeypatch):
    """写报告**不带 tools**：模型这一轮没有工具可调，只能写字。

    带 tools 的话模型可能返回一个工具调用而不是报告 —— 而报告这一轮只想要文字。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    calls = _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert calls[0]["tools"] is None


# ---------------------------------------------------------------------------
# 5. 一次重写机会（§5.3 第 6 行 / §3.5.2）
# ---------------------------------------------------------------------------


def test_fabricated_number_triggers_exactly_one_rewrite(basket, monkeypatch):
    """编造的数字 → 给模型**一次**重写机会；改好了就 `passed=True`。

    这条同时钉住"只给一次"：`chat` 一共调两次（第一次写、第二次重写），
    第三次不存在。`if` 写成 `while` 时这条会红。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    bad = _good_sections()
    bad["sections"][0]["text"] += "另外退货率高达 88.8%。"
    calls = _install_chat(
        monkeypatch,
        [
            json.dumps(bad, ensure_ascii=False),
            json.dumps(_good_sections(), ensure_ascii=False),
        ],
    )

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert len(calls) == 2, "第一次写 + 一次重写，正好两次"
    assert state.validation is not None
    assert state.validation.passed is True
    assert state.validation.unsupported_numbers == []
    assert state.status is WorkflowStatus.SUCCESS


def test_rewrite_note_tells_the_model_which_numbers_were_wrong(basket, monkeypatch):
    """重写轮要**把上一版正文贴回去**，并说清哪个数字没出处。"""
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    bad = _good_sections()
    bad["sections"][0]["text"] += "另外退货率高达 88.8%。"
    calls = _install_chat(
        monkeypatch,
        [
            json.dumps(bad, ensure_ascii=False),
            json.dumps(_good_sections(), ensure_ascii=False),
        ],
    )

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    rewrite_messages = calls[1]["messages"]
    roles = [message["role"] for message in rewrite_messages]
    assert roles[-2:] == ["assistant", "user"], "要贴回上一版，再追加一条提示"
    assert "88.8" in rewrite_messages[-1]["content"], "要点名是哪个数字没出处"


def test_still_wrong_after_rewrite_keeps_the_report_and_degrades_status(basket, monkeypatch):
    """重写之后**还是**编造 → 保留原文、状态降级，但**绝不 FAILED**。

    这是 §3.5.2「再不过就保留原文但把 status 降级」的完整落地：
    三次都返回同一份编造的报告，`chat` 仍然只调两次（只给一次重写），
    最终 `SUCCESS_WITH_WARNINGS` 且 `validation.passed=False`。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    bad = _good_sections()
    bad["sections"][0]["text"] += "另外退货率高达 88.8%。"
    calls = _install_chat(monkeypatch, [json.dumps(bad, ensure_ascii=False)] * 3)

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert len(calls) == 2, "只给一次重写机会"
    assert state.status is WorkflowStatus.SUCCESS_WITH_WARNINGS
    assert state.status is not WorkflowStatus.FAILED
    assert state.validation is not None
    assert state.validation.passed is False
    assert "88.8" in state.validation.unsupported_numbers
    assert state.report is not None
    assert "88.8" in state.report.sections[0].text, "再不过就保留原文"


def test_rewrite_is_not_offered_when_the_report_is_the_template(basket, monkeypatch):
    """模板报告不会被"重写"：它的数字由代码生成，不存在编造。

    这条防的是"重写判断只看 `validation.passed`"：模板路径下
    `plan.source == "rule_fallback"`，`should_rewrite_report` 必须返回 False。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch, configured=True)
    _install_chat(monkeypatch, [])

    state = _llm_plan_state(dataset_id)
    run_workflow(state)  # 规则兜底计划 + 模板报告

    assert state.report is not None
    state.validation = validate(state)
    assert should_rewrite_report(state) is False


def test_should_rewrite_report_needs_a_validation_result(basket):
    """还没校验过（`state.validation is None`）→ 不重写。"""
    _, dataset_id = basket
    state = _llm_plan_state(dataset_id)
    state.plan = _llm_plan(dataset_id)

    assert state.validation is None
    assert should_rewrite_report(state) is False


def test_rewrite_is_not_offered_when_the_model_is_no_longer_configured(basket, monkeypatch):
    """报告是模型写的、校验也查出了编造数字，但**配置已经没了** → 仍然不重写。

    这条补的是"模板报告不重写"那条用例够不到的一格：它跑的是规则兜底计划，
    `validation.passed=True` 且 `issues=[]`，所以"重写判断里那句
    `if not should_try_llm_report(state)`"删掉它也不会红。
    要钉住那句，必须造出**两个条件相反**的状态：报告确实是模型写的、
    校验确实有编造数字，但 `should_try_llm_report` 为假。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)
    assert state.report is not None
    assert state.report.summary.startswith(LLM_WROTE_NOTICE), "前提：这份报告是模型写的"

    # 配置被清掉（key 过期、环境变量没带上、演示时切到规则模式），但状态里那份
    # 模型写的报告和编造数字都还在 —— 这时候再去调模型是发不出去的。
    _configure_llm(monkeypatch, configured=False)
    state.validation = _validation_with("UNSUPPORTED_NUMBER")

    assert should_try_llm_report(state) is False
    assert should_rewrite_report(state) is False


def test_rewrite_is_not_offered_for_other_error_level_issues(basket, monkeypatch):
    """别的 error 级问题（任务失败、图丢了）**不触发重写**：口径不能放大。

    重写只能改正文里的数字，改不了"某个任务失败了"这个事实。把口径放大成
    `not passed` 的结果是白烧一次模型额度、报告却一个字没改。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    _install_chat(monkeypatch, [json.dumps(_good_sections(), ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)
    assert state.report is not None
    assert state.report.summary.startswith(LLM_WROTE_NOTICE), "前提：这份报告是模型写的"
    assert should_try_llm_report(state) is True, "前提：模型这条路是通的"

    state.validation = _validation_with("FAILED_TASK")

    assert state.validation.passed is False, "前提：确实有一条 error 级问题"
    assert should_rewrite_report(state) is False


# ---------------------------------------------------------------------------
# 6. 解析与兜底：`write` 自己的契约（恒不抛异常、空段丢掉）
# ---------------------------------------------------------------------------


def test_write_returns_none_instead_of_raising_on_bad_json(basket, monkeypatch):
    """模型给了一段抠不出 JSON 的文本 → `write` 返回 `None`，**不抛异常**。

    为什么必须直接调 `write` 而不是跑一遍工作流：`_build` 里还有一层 `try/except`，
    跑工作流的话"抛异常"和"返回 None"看起来一模一样（都退回模板）——
    这一条测的是 `write` 自己的契约，不是调用方的兜底。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    # 文本里**有**一对花括号，所以会走到"抠出来再解一次"那条路 —— 那才是坏 JSON 的兜底；
    # 只写"这不是 JSON"的话会提前从"找不到花括号"那条路返回，兜底代码根本没被执行到。
    _install_chat(monkeypatch, ["好的，这是报告：{这不是合法的 JSON}"])

    state = _llm_plan_state(dataset_id)
    state.plan = _llm_plan(dataset_id)

    assert report_llm.write(state, None) is None


def test_write_returns_none_instead_of_raising_when_the_call_fails(basket, monkeypatch):
    """调用阶段就炸了（超时 / 401 / 网络断）→ `write` 返回 `None`，**不抛异常**。

    与 `test_model_call_failure_falls_back_to_the_template` 分开：那条跑完整工作流，
    证明的是"编排器会退回模板"；这条直接调 `write`，证明的是"它自己没有把异常扔出去"。
    少了这一条，`write` 里那层 `try/except` 整段删掉也不会有用例变红。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    _install_failing_chat(monkeypatch, TimeoutError("模型超时"))

    state = _llm_plan_state(dataset_id)
    state.plan = _llm_plan(dataset_id)

    assert report_llm.write(state, None) is None


def test_blank_section_is_dropped_while_the_rest_is_kept(basket, monkeypatch):
    """空白的段要**丢掉**，不能变成报告里一个只有标题的空壳。

    这条不能写成"整份退回模板"：其余三段都是合格的，丢掉空段之后报告照样能用。
    只留一个空段的话会被长度下限（`_MIN_REPORT_CHARS`）挡掉，那样就分不清
    "空段被丢掉"和"整份太短被丢掉"了 —— 而这两件事的修法完全不同。
    """
    _, dataset_id = basket
    _configure_llm(monkeypatch)
    payload = _good_sections()
    payload["sections"].insert(
        0, {"heading": "这一段是空的", "text": "   \n   ", "cites": ["T1"]}
    )
    _install_chat(monkeypatch, [json.dumps(payload, ensure_ascii=False)])

    state = _start(monkeypatch, dataset_id, _llm_plan(dataset_id))
    run_workflow(state)

    assert state.report is not None
    assert state.report.summary.startswith(LLM_WROTE_NOTICE), "其余段落合格，报告仍然用模型写的正文"
    assert all(section.text.strip() for section in state.report.sections), "空壳段必须被丢掉"
    assert "这一段是空的" not in [section.heading for section in state.report.sections]


# ---------------------------------------------------------------------------
# 7. 辅助函数
# ---------------------------------------------------------------------------


def _llm_plan(dataset_id: str) -> TaskPlan:
    """一个 `source="llm"` 的计划（模型报告路径的前提条件）。

    只放一个任务：本文件测的是"报告怎么写"，任务越多越难一眼看出报告里的数字
    是从哪来的（那是 `tests/test_validation_numbers.py` 的范围）。
    """
    return _plan(dataset_id, source="llm")


def _rule_plan(dataset_id: str) -> TaskPlan:
    """一个 `source="rule_fallback"` 的计划（报告必须走模板路径）。"""
    return _plan(dataset_id, source="rule_fallback")


def _plan(dataset_id: str, source: str) -> TaskPlan:
    """两种来源共用的计划构造：同一个任务，只有 `source` 不同。

    共用一份是为了让"报告路径的差别**只**来自 source"这件事在代码里看得见 ——
    两个函数各写一遍的话，改任务参数时很容易只改一处，于是对比就失效了。
    """
    return TaskPlan(
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
        reasoning="由大模型规划：1 个任务（模型 test-model）"
        if source == "llm"
        else "命中规则：分组汇总（规则兜底，未使用大模型）",
        created_at=_now(),
    )
