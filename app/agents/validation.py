"""报告生成与数字溯源。

D4 只实现了 `build_report(state, validation=None)`（模板拼装，零 LLM）；
D5 加上 `validate(state) -> ValidationResult`（数字溯源，抓编造）和"模型重写
报告正文"的路径。

## 为什么模板报告不是"临时凑数"

架构文档 §5.4 第 3 条把它定成**失败收尾的一部分**：模型不可用时报告必须还能出来。
理由很实际 —— 演示时 API Key 可能还没配好，如果没配就没有报告，
那"完整链路"这件事当天就演示不了。所以模板路径是**产品功能**，不是降级补丁。

## `llm_used` 的口径（D5 修订，原先是硬编码 False）

这个字段是"这次到底有没有用模型"的**诚实标记**（`app/schemas_workflow.py:210`），
所以它必须按 `state.plan.source` 推导，不能写死：

    - `rule_fallback` → False（模型没参与，或调失败退回了规则）
    - `llm`          → True（这轮的计划是模型给的）

配套地，`summary` 首段也跟着换：没模型 → 逐字的 TEMPLATE_NOTICE；
模型参与了规划但正文仍是模板 → 逐字的 LLM_PLANNED_NOTICE。
**两者都是常量**，因为这两句话要被测试逐字断言（验收标准 ① ② 都钉了它们）。
D4 时这里写死 `False`，导致真 key 规划成功后报告说"未使用大模型"——
数字全对，但"哪一步用了模型"这句陈述是假的，这恰是验收标准 ② 要抓的东西。

## 两条硬约束

1. **数字只能来自 `state.results`**：本文件里**没有任何聚合计算**，
   连 `sum()` 都不对数据做。表格是 `ToolResult.data["rows"]` 的**直接复制**，
   所以网页上的表格和工具输出必然一致（D5 的溯源校验才可能过）。
2. **绝不抛异常**：报告生成失败就没有报告可给，而"没有报告"会让前端
   什么都显示不出来。所以外面包了一层兜底，最坏也给一个只有一句话的合法报告。
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app import config
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    FinalReport,
    ReportSection,
    TaskStatus,
    ValidationIssue,
    ValidationResult,
    WorkflowState,
)

logger = logging.getLogger(__name__)

# 逐字写死的一句话。它是"诚实标记"的载体，所以用常量而不是在函数里拼字符串 ——
# 拼字符串意味着有一天有人会顺手改两个字，而这句话是要被测试断言的。
TEMPLATE_NOTICE = "本次未使用大模型（llm_used=false），以下结论为工具结果的直接汇总"

# 模型参与了规划、但正文仍是模板时用的诚实标记。**不能省**：
# D4 实测过，真 key 规划成功后报告首段若仍写"未使用大模型"，用户会以为这次
# 全程没用模型（验收标准 ② 要求"写明哪一步用了模型、哪一步没用"）。
LLM_PLANNED_NOTICE = (
    "本次由大模型参与规划（选择了分析工具与参数），"
    "但报告正文是模板自动汇总的，以下结论仍为工具结果的直接汇总"
)

# 模型真的写了正文时用的诚实标记。**第三个常量不是多余的**：
# `LLM_PLANNED_NOTICE` 里有半句话是"报告正文是模板自动汇总的"。D5 加了
# `report_llm.write()` 之后，模型可能真的写了正文 —— 这时再复用那句话，
# 就是 D-22 记的那类"数字全对、陈述是假的"（首段说正文是模板写的，
# 而用户眼前这几段明明不是模板写的）。三句话互斥、必须逐字为真，
# 所以三句话都是常量，且要被测试逐字断言。
LLM_WROTE_NOTICE = (
    "本次报告正文由大模型根据工具结果撰写，"
    "其中所有数字均来自下方引用的工具调用，可逐条核对"
)

# 表格最多复制多少行。20 行足够在网页上读完，再多就变成"把工具输出原样倒出来"。
_MAX_TABLE_ROWS = 20


def build_report(
    state: WorkflowState, validation: ValidationResult | None = None
) -> FinalReport:
    """把 `state.results` 拼成一份 `FinalReport`。**不抛异常。**

    它**只读** `state`（不改任何字段）：改状态是 Orchestrator 的事（§3 第 1 条）。

    `validation` 是 D5 的重写路径参数：第一次写报告时还没有校验结果
    （先有报告才谈得上校验报告里的数字），所以默认 `None`；
    校验发现问题后由 Orchestrator 带着 `ValidationResult` 再调一次，
    这时 `summary` 里要写清"哪些数字被复核过"。
    """
    try:
        return _build(state, validation)
    except Exception:
        # 真到了这里说明模板代码自己有 bug（比如某个字段是意料外的类型）。
        # 兜底不是"掩盖问题"：日志里有完整堆栈，而用户至少还能看到一句
        # "报告生成遇到问题" —— 比前端空白页好，也比让异常逃出去把整条工作流
        # 判成 FAILED 好（任务其实都算完了）。
        logger.exception("生成报告时出现未预期的错误，已退回最小报告")
        return _minimal_report(state)


def _build(
    state: WorkflowState, validation: ValidationResult | None = None
) -> FinalReport:
    """正常路径：优先让模型写正文，失败就退回模板（模板路径的实现在下面）。"""
    if should_try_llm_report(state):
        # import 写在函数里：`app.llm.client` 会 import openai，放模块顶层的话
        # "openai 装不上/版本不对"会让整个 validation 模块 import 失败 ——
        # 连完全不碰模型的模板报告一起挂掉（与 `planner._ask_model` 同一个理由）。
        from app.agents import report_llm

        try:
            report = report_llm.write(state, validation)
            if report is not None:
                return report
        except Exception:
            # `report_llm.write` 自己已经兜了一层（契约是恒不抛异常），真到了这里
            # 说明它内部有 bug。模型这条路出问题**不是分析失败**：退回模板，
            # 用户照样拿到一份数字齐全的报告（§5.4 第 3 条）。
            logger.exception("模型撰写报告失败（未预期的错误），改用模板报告")

    return _template_report(state)


def should_try_llm_report(state: WorkflowState) -> bool:
    """这次要不要让模型写报告正文。**两个条件缺一不可。**

    ① `plan.source == "llm"`：规则兜底计划意味着模型要么没配、要么调失败了。
       这种情况下再去调模型写报告是自相矛盾的 —— 报告正文说"由大模型撰写"，
       而这次分析从头到尾没用上模型。而且它会把"没配 key"的演示路径变成每次提问
       都白等一次超时。
    ② `llm_is_configured()`：配置齐了才调。光看 `plan.source` 不够 ——
       测试和演示里常常是"配置被清掉、但状态是手工造出来的"，真发一次请求
       就是真的花钱、真的等超时。

    这个判断放在 `validation.py`（而不是 Orchestrator）是因为有一条 AST 测试钉着
    "Orchestrator 不许 import `app.llm`"：状态机不该知道模型怎么调。
    """
    if state.plan is None or state.plan.source != "llm":
        return False

    from app.agents.planner import llm_is_configured

    return llm_is_configured()


def should_rewrite_report(state: WorkflowState) -> bool:
    """要不要让模型**重写一版**报告（§5.3 第 6 行：发现编造数字 → 重写 1 次）。

    两个条件：① 现在这份报告是模型写的；② 校验确实在正文里查出了编造的数字。

    **"只给一次"不在这里判断**，而是由调用方保证：Orchestrator 的 validation 阶段
    只有一条 `if should_rewrite_report(state):`，不是循环 —— 重写完之后无论结果如何
    都直接 `_finish`（`tests/test_report_llm.py` 里有用例钉住"整条工作流只写两次报告"）。
    这里刻意**不**用"报告里还留着上一版被点名的数字吗"这类内容判断来兼职数次数：
    那是拿一个近似值当计数器用，模型换个说法绕过去就会失效。
    """
    report = state.report
    validation = state.validation
    if report is None or validation is None:
        return False
    if not should_try_llm_report(state):
        # 报告是模板写的：模板的数字由代码生成，重写它没有意义（模板里也不会有
        # 编造的数字）。这一条同时挡住了"模型没配、但状态被手工造成 llm"的情况。
        return False

    return any(
        issue.code == "UNSUPPORTED_NUMBER" and issue.severity == "error"
        for issue in validation.issues
    )


def _template_report(state: WorkflowState) -> FinalReport:
    """模板路径：每个成功的任务一段，外加数据质量说明。**零 LLM。**"""
    sections: list[ReportSection] = []
    citations: list[str] = []

    for result in _latest_results(state):
        if not result.ok:
            continue
        sections.append(_section_for(result, state))
        citations.append(result.task_id)

    if not sections:
        # 一个成功的任务都没有（全失败，或者计划是空的）。`FinalReport.sections`
        # 有 `min_length=1`，所以这里必须给一段 —— 而且要说清"为什么没有结果"，
        # 不能给一句空洞的"暂无数据"。
        sections.append(
            ReportSection(
                heading="没有可用的分析结果",
                text=(
                    "这次分析没有产生任何成功的结果，因此没有可以汇总的数字。"
                    f"计划里一共 {_planned_count(state)} 个任务，全部失败了。"
                    "每个任务失败的具体原因可以在任务列表里看到。"
                ),
                cites=[],
                table=None,
            )
        )

    return FinalReport(
        report_id=uuid4().hex[:8],
        title=f"分析报告：{_clip(state.question, 60)}",
        summary=_summary(state, citations),
        sections=sections,
        data_quality_notes=_quality_notes(state),
        citations=citations,
        generated_at=datetime.now(timezone.utc),
        llm_used=_llm_used(state),
    )


def _llm_used(state: WorkflowState) -> bool:
    """**这次到底有没有用模型** —— `FinalReport.llm_used` 的唯一来源。

    这里曾经硬编码 `False`，理由是"D4 只有模板路径"。但那句话本身变成了假陈述：
    真 key 接入后模型确实参与了**规划**（`plan.source == "llm"`），
    而报告首段、接口 7 的 `llm_used`、前端横幅三处都会显示"未使用大模型"。
    数字一个没错，错的是"哪一步用了模型"这句陈述 —— 而 `llm_used` 在
    `app/schemas_workflow.py:210` 的 docstring 里被定义为"这次到底有没有用模型"的
    **诚实标记**，两头必须对上。

    口径（只认 `plan.source`，不猜）：
    - `rule_fallback` → False：模型没参与（要么没配，要么调失败退回了规则）。
    - `llm` → True：这轮的计划是模型给的。
    - 计划还没生成 → False：走到这里说明比规划更早的地方就失败了。
    """
    if state.plan is None:
        return False
    return state.plan.source == "llm"


def _latest_results(state: WorkflowState) -> list:
    """按 task_id 去重，同一个任务**只保留最后一次**的结果。

    为什么会有重复：重试过的任务在 `state.results` 里留了两条（第一次失败 + 第二次）。
    两条都留痕是对的（排查时要知道第一次报了什么），但报告里出现两段同样标题的内容
    就成了噪音。取"最后一次"是因为它才是这个任务最终的结论。
    """
    latest: dict[str, Any] = {}
    for result in state.results:
        latest[result.task_id] = result  # 后写覆盖先写 = 保留最后一次
    # 按 plan 里的顺序输出，让报告的段落顺序和用户看到的任务顺序一致
    order = [task.task_id for task in (state.plan.tasks if state.plan else [])]
    ordered = [latest[task_id] for task_id in order if task_id in latest]
    # plan 里没有的任务（理论上不该有）也别丢掉，追加在最后
    ordered.extend(result for task_id, result in latest.items() if task_id not in order)
    return ordered


def _section_for(result: Any, state: WorkflowState) -> ReportSection:
    """一个任务 → 一段报告。标题用计划里的中文描述，正文用工具返回的真实数字。"""
    task = _find_task(state, result.task_id)
    heading = task.description if task is not None else f"任务 {result.task_id}"

    text = f"{_summarize(result)}（工具：{result.tool_name}，任务：{result.task_id}）"

    return ReportSection(
        heading=heading,
        text=text,
        cites=[result.task_id],
        table=_copy_table(result.data),
    )


def _summarize(result: Any) -> str:
    """把工具返回的结构翻译成一句中文。

    **这里不做任何计算**，只做两件事：读已经算好的字段、把它们放进句子里。
    一旦在这里出现 `sum(...)` / `mean(...)`，报告里的数字就不再是工具算的那个，
    D5 的溯源校验会当场抓出来（`UNSUPPORTED_NUMBER`）。
    """
    data = result.data
    tool = result.tool_name

    if tool == "missing_values":
        return _summarize_missing(data)
    if tool == "describe_dataset":
        return _summarize_describe(data)
    if tool == "correlation":
        return _summarize_correlation(data)
    if tool == "time_series_summary":
        return _summarize_timeseries(data)
    # group_aggregate / top_n 以及将来新增的"有 rows 的工具"都走这条通用路径
    return _summarize_rows(data)


def _summarize_missing(data: dict[str, Any]) -> str:
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        return "没有取到缺失值统计。"

    with_nulls = [c for c in columns if isinstance(c, dict) and c.get("null_count")]
    duplicate = data.get("duplicate_row_count")

    if not with_nulls:
        sentence = f"共检查 {len(columns)} 列，没有发现缺失值。"
    else:
        # 只报前 3 列：缺失列可能有几十个，全列出来报告就没法读了
        parts = [
            f"{c.get('column')}（{_num(c.get('null_count'))} 个，占 {_percent(c.get('null_ratio'))}）"
            for c in with_nulls[:3]
        ]
        sentence = f"共检查 {len(columns)} 列，其中 {len(with_nulls)} 列有缺失值：" + "、".join(parts)
        if len(with_nulls) > 3:
            # 这里**故意不写"另有 N 列"**：N = len(with_nulls) - 3 是展示层算出来的，
            # 工具结果里没有这个数，`validate()` 会（正确地）把它当成找不到出处的数字。
            # 与其为了让校验放行而放宽判据，不如让报告只说它有出处的话 ——
            # "见下方表格"把信息留住了，表格里就是完整的 9 行列缺失统计。
            sentence += "，其余有缺失的列见下方表格"

    if duplicate:
        sentence += f"；完全重复的行有 {_num(duplicate)} 行。"
    return sentence


def _summarize_describe(data: dict[str, Any]) -> str:
    stats = data.get("stats")
    if not isinstance(stats, list) or not stats:
        return f"数据共 {_num(data.get('row_count'))} 行，没有可做描述性统计的数值列。"

    parts = []
    for stat in stats[:3]:
        if not isinstance(stat, dict):
            continue
        parts.append(
            f"{stat.get('column')} 的均值是 {_num(stat.get('mean'))}、"
            f"中位数是 {_num(stat.get('median'))}、"
            f"范围是 {_num(stat.get('min'))} 到 {_num(stat.get('max'))}"
        )
    return f"数据共 {_num(data.get('row_count'))} 行。" + "；".join(parts) + "。"


def _summarize_correlation(data: dict[str, Any]) -> str:
    """挑"绝对值最大的一对"来说，不是挑第一个。

    `app/tools/compare.py:76-85` 的 `pairs` 是**按列序**构造的（上三角遍历），
    没有任何排序 —— 所以 `pairs[0]` 只是"第一对列"，跟相关性强不强没有关系。
    直接说 `pairs[0]` 会把"最弱的一对"包装成"最强的一对"：
    sales.csv 上 `pairs[0]` 是 销量/单价（r=-0.0457），而真正最强的是
    销售额/利润（r=0.9872）。**这句话的数据是真的，但"最强"这个判断是假的。**

    排序也不归报告做（§3.5"不做计算"）—— 这里做的只是"挑一个已有的元素"，
    比较的是工具已经算好的 `r`，不产生任何新的数字。
    """
    pairs = data.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        return "没有算出可用的相关系数（可能数值列不足 2 列）。"

    valid = [p for p in pairs if isinstance(p, dict) and p.get("r") is not None]
    if not valid:
        return "没有算出可用的相关系数（所有列对都缺一个实数相关系数）。"

    # `r` 可能是负数，所以按绝对值取最大；并列时取先出现的那一对（max 的稳定性）。
    top = max(valid, key=lambda item: abs(_as_float(item.get("r"))))

    r = top.get("r")
    return (
        f"相关性最强的一对是 {top.get('x')} 与 {top.get('y')}，"
        f"相关系数 {_num(r)}（共比较了 {len(valid)} 对）。"
    )


def _summarize_timeseries(data: dict[str, Any]) -> str:
    rows = data.get("rows")
    columns = data.get("columns")
    if not isinstance(rows, list) or not rows or not isinstance(columns, list) or not columns:
        return "没有取到可用的时间序列数据。"

    first, last = rows[0], rows[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        return "时间序列结果的结构不是预期形状。"

    label, value = columns[0], columns[-1]
    sentence = (
        f"时间从 {first.get(label)} 到 {last.get(label)}，"
        f"{value} 由 {_num(first.get(value))} 变为 {_num(last.get(value))}"
    )
    skipped = data.get("skipped_row_count")
    if skipped:
        sentence += f"；有 {_num(skipped)} 行因为日期解析不出来被跳过"
    return sentence + "。"


def _summarize_rows(data: dict[str, Any]) -> str:
    """`group_aggregate` / `top_n` 的通用说法：拿第一行（也就是排最前的那组）。"""
    rows = data.get("rows")
    columns = data.get("columns")
    if not isinstance(rows, list) or not rows:
        return "这次没有返回任何数据行。"
    if not isinstance(columns, list) or not columns:
        return f"这次返回了 {len(rows)} 行数据，但没有给出列名。"

    first = rows[0]
    if not isinstance(first, dict):
        return f"这次返回了 {len(rows)} 行数据，但行的结构不是预期形状。"

    # 最多说 4 个字段：分组列 + 指标列通常就 2–3 个，多说的都是噪音
    parts = [f"{name} = {_num(first.get(name))}" for name in columns[:4]]
    return f"排在最前面的一组是 " + "，".join(parts) + f"（共 {len(rows)} 行结果）。"


def _summary(
    state: WorkflowState, citations: list[str], model_wrote: bool = False
) -> str:
    """总述：第一段是**逐字**的诚实标记，第二段是这次跑成了什么。

    第一段用哪句取决于"模型到底参与了哪一步"，**三选一**：

    | 这次发生了什么 | 首段 |
    |---|---|
    | 全程没有模型 | `TEMPLATE_NOTICE` |
    | 模型只做了规划，正文是模板汇总的 | `LLM_PLANNED_NOTICE` |
    | 正文是模型写的 | `LLM_WROTE_NOTICE` |

    三句话都是常量，因为**这句话是要被测试逐字断言的**（验收标准 ① ② 都钉了它）。

    `model_wrote` 由调用方给，而不是在这里从 `state` 猜：只有 `report_llm._assemble`
    知道"这几段是模型写的"，而 `_llm_used` 只知道"计划是模型给的"。两件事必须分开 ——
    模型规划成功但写报告失败时，正文仍然是模板写的，此时首段必须是
    `LLM_PLANNED_NOTICE`（"参与规划、正文是模板"），不能因为 `llm_used=True`
    就说"正文由大模型撰写"。
    """
    tasks = state.plan.tasks if state.plan else []
    failed = [task for task in tasks if task.status.value == "FAILED"]
    skipped = [task for task in tasks if task.status.value == "SKIPPED"]

    if model_wrote:
        notice = LLM_WROTE_NOTICE
    else:
        notice = LLM_PLANNED_NOTICE if _llm_used(state) else TEMPLATE_NOTICE

    lines = [notice]
    lines.append(
        f"针对问题「{state.question}」，系统一共规划了 {len(tasks)} 个分析任务，"
        f"成功 {len(citations)} 个。"
    )

    if failed or skipped:
        # §5.4 第 5 条：报告里要写清"哪几项没能完成"，不能只报喜
        parts = []
        if failed:
            parts.append("失败：" + "、".join(task.description for task in failed[:3]))
        if skipped:
            parts.append("跳过：" + "、".join(task.description for task in skipped[:3]))
        lines.append("未能完成的任务 —— " + "；".join(parts) + "。")

    return "\n\n".join(lines)


def _quality_notes(state: WorkflowState) -> list[str]:
    """数据质量提示：**全部来自画像**，不由模板"编"。

    画像的 `warnings` 是 profiler 算出来的原话，直接抄；另外补两条用户最关心的
    事实（有多少列有缺失、有没有重复行），它们同样取自画像字段，不是新算的。
    """
    profile = state.profile
    if profile is None:
        return []

    notes: list[str] = list(profile.warnings)

    columns_with_nulls = [c for c in profile.columns if c.null_count > 0]
    if columns_with_nulls:
        names = "、".join(c.name for c in columns_with_nulls[:5])
        note = f"有 {len(columns_with_nulls)} 列存在缺失值：{names}"
        if len(columns_with_nulls) > 5:
            note += " 等"
        # 画像的 warnings 里可能已经说过同一件事（profiler 会为高缺失列写提示），
        # 那就不要再重复一遍 —— 重复的提示会让用户以为有两个不同的问题。
        if not any(note[:8] in warning for warning in notes):
            notes.append(note)

    if profile.duplicate_row_count and not any(
        "重复" in warning for warning in notes
    ):
        notes.append(f"有 {profile.duplicate_row_count} 行完全重复的记录")

    return notes


def _copy_table(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    """**直接复制**工具结果里的行，最多 20 行。

    按 `rows` → `stats` → `columns` → `pairs` 的顺序找：不同工具的叫法不同，
    但都是"一个 dict 组成的列表"，直接抄进表格即可。**绝不重算、绝不改数值精度** ——
    D5 的数字溯源校验会把报告里的每个数字拿回工具结果里比对，改一位小数就对不上。
    """
    for key in ("rows", "stats", "columns", "pairs"):
        value = data.get(key)
        if not isinstance(value, list) or not value:
            continue
        rows = [item for item in value if isinstance(item, dict)]
        if rows:
            return [dict(row) for row in rows[:_MAX_TABLE_ROWS]]
    return None


def _find_task(state: WorkflowState, task_id: str) -> Any:
    """按 task_id 找回计划里的任务（为了拿它的中文描述当标题）。"""
    if state.plan is None:
        return None
    for task in state.plan.tasks:
        if task.task_id == task_id:
            return task
    return None


def _planned_count(state: WorkflowState) -> int:
    return len(state.plan.tasks) if state.plan else 0


def _minimal_report(state: WorkflowState) -> FinalReport:
    """最后一道兜底：只有一句话，但**形状完全合法**（能被 `FinalReport` 校验通过）。

    它存在的意义只有一个：`build_report` 的契约是"绝不抛异常"。返回 None 会让
    Orchestrator 以为"报告没生成"，而实际上我们只是拼装时出了 bug —— 两者要分开。
    """
    notice = LLM_PLANNED_NOTICE if _llm_used(state) else TEMPLATE_NOTICE
    return FinalReport(
        report_id=uuid4().hex[:8],
        title=f"分析报告：{_clip(state.question, 60)}",
        summary=notice + "\n\n生成报告时遇到问题，只保留了基本说明。",
        sections=[
            ReportSection(
                heading="报告生成遇到问题",
                text="分析任务已经执行完毕，但把结果整理成报告时出错了。请查看服务端日志。",
                cites=[],
                table=None,
            )
        ],
        data_quality_notes=[],
        citations=[],
        generated_at=datetime.now(timezone.utc),
        llm_used=_llm_used(state),
    )


def _as_float(value: Any) -> float:
    """把工具结果里的数字读成 float；读不了按 0 处理（只用于**排序**，不进报告）。

    这里出现 float 转换会不会违反"报告不做计算"？不会：转换本身不产生新数字，
    报告里出现的 `r` 仍然是 `top.get("r")` 的原值（见 `_num` 的格式化）。
    `_as_float` 唯一的用途是给 `abs()` 一个可比较的量 —— 模型不参与这个判断。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _num(value: Any) -> str:
    """数字的展示格式。`None` 显示成"无"而不是"None"——报告是给人看的。

    千分位与两位小数是**纯展示**，不改变数值本身：表格里仍然是工具返回的原值，
    只有这句自然语言里出现四舍五入的写法。这一点很重要 —— D5 的溯源校验
    会同时接受"绝对值相等"和"四舍五入到 0/1/2 位后相等"，所以展示格式不会误报。
    """
    if value is None:
        return "无"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _percent(value: Any) -> str:
    """把 0–1 的占比写成百分比。拿不到数字时返回"未知"，不猜。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "未知"
    return f"{value * 100:.1f}%"


def _clip(text: str, limit: int) -> str:
    """截断过长的文本。用"…"结尾，让人一眼看出是被截断的。"""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# D5：数字溯源校验（`validate`）
#
# 这一步回答的问题是："报告里那些数字，真的都是工具算出来的吗？"
# 它是这套系统"不编数字"这条承诺的**最后一道防线**：规划靠工具白名单 + 参数校验，
# 执行靠 `app/tools/validate.py`，报告靠这里。
#
# ## 两把尺子
#
# 报告里的数字分两类，出处不同，所以判据也必须不同：
#
# 1. **数值**（工具算出来的，例如 `24,871,657.00`、`0.99`）：必须能在某个
#    `ToolResult.data` 里找到出处。
# 2. **规模量**（报告自己数出来的，例如"共 6 行结果""共 5 列有缺失值"）：它们是
#    **数**出来的，不是算出来的 —— `len(rows)`、某个键"有值"的条数。这类数字
#    不会以数值的形式出现在 data 里，所以允许集里必须包含"结构计数"。
#    不这么做的话，诚实的模板报告会被自己抓成编造，那这个校验就没人信了。
#
# ## 为什么允许"四舍五入后相等"
#
# `_num` 把 float 写成两位小数、`_percent` 把占比写成一位小数的百分比，所以报告
# 里的 `0.99` 对应 data 里的 `0.9872`、`1.1` 对应 `0.0111` —— 它们是**同一个事实
# 的不同写法**，不是编造。只认字面相等的话模板报告会满屏误报。
# 反过来说，编造的数字（比如"利润率 47.5%"）也不会因为这条容差蒙混过关：
# 它得先有一个真的数据点落在它附近才行。
#
# ## 为什么不校验 `report.summary`
#
# 契约（架构文档 §3.5.1 与任务书）点名的是 `sections[].text`。这也有道理：summary
# 里的数字是"规划了几个任务、成功几个"，属于**计划层面**的计数，不来自任何一次
# 工具调用；把它一起校验就得为它单独造一套允许集，反而模糊了"数字必须有工具出处"
# 这条主线。sections 才是结论所在。
# ---------------------------------------------------------------------------

# 报告"短得像没写完"的判据（正文总字符数 = summary + 所有 section）。
# 阈值是**量出来的**，不是拍的（`.tmp/probe_d5_short_forms.py` 实测）：
#   - 真实模板报告：158 字（最短的单任务）～ 198 字（缺失值那条最长）
#   - 所有任务都失败时的报告：188 字（那段"没有可用的分析结果"本身不短）
#   - `_minimal_report()` 兜底：94 字 —— 报告生成那一步炸了才会有它
# 取 120：兜底的 94 会被记一条 warning（这是对的，"报告生成遇到问题"值得被看见），
# 而真实报告最短的 158 离它还有 38 字的余量，不会误报。
_MIN_REPORT_CHARS = 120

# 数字的抽取规则。前面那个负向后顾 `(?<![A-Za-z_0-9])` 是必需的：没有它，
# 段尾的"任务：T1"里的 1 会被当成一个数字抽出来，而它根本不是数据结论，
# 于是每一份报告都会凭空多出一个"找不到出处"的数字。前面是数字时也挡掉，
# 是为了让 `2024-01` 里的 `01` 单独被识别成 1（而不是把 `-01` 连起来）。
_NUMBER_RE = re.compile(r"(?<![A-Za-z_0-9])(-?\d[\d,]*(?:\.\d+)?)")

# 字符串里嵌着的数字也算出处：时间序列的期间标签 `2024-01` 里的 2024 和 1
# 就是这个来源，报告正文写"时间从 2024-01 到 2024-12"是完全诚实的。
_EMBEDDED_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# "某列 的 某统计量"这个句式（模板的 `_summarize_describe` 就是它）。
_COLUMN_STAT_RE = re.compile(
    r"([^\s，。、；：（）()\[\]{}]+)\s*的(?:均值|中位数|平均数|最大值|最小值|标准差|方差)"
)

# "列名 = 值"这个句式（模板的 `_summarize_rows` 就是它）。
_COLUMN_ASSIGN_RE = re.compile(r"([^\s，。、；：（）()\[\]{}]+)\s*=\s*")

# 工具会拼出来的聚合列后缀（`销售额_sum` 这种）。它们不是画像里的列，
# 但它们是**真实存在的工具输出列**，不能算"编造的列名"。
_AGG_SUFFIXES = ("sum", "mean", "count", "min", "max", "median")


def validate(state: WorkflowState) -> ValidationResult:
    """核对报告里的数字与列名，返回一份校验结果（**完全不调大模型**）。

    这一步只**读** `state`，不改报告、不重跑工具、不决定工作流状态 ——
    决定状态是 Orchestrator 的事（架构文档 §3.5.1 的三条"不能做"）。
    `passed` 的口径：没有任何 `severity == "error"` 的问题。
    warning 只是提醒（例如某个任务返回 0 行），不推翻整份报告。

    **绝不抛异常**：校验本身出错时返回一份 `passed=False` 的最小结果。
    这里不能让它把工作流带崩 —— 校验是"最后一道防线"，防线自己塌了要留下痕迹，
    而不是把已经算好的分析结果一起毁掉。
    """
    try:
        return _validate(state)
    except Exception as exc:  # noqa: BLE001 —— 校验层刻意兜住一切异常，见上面的理由
        logger.exception("数字溯源校验本身出错了")
        return ValidationResult(
            passed=False,
            checked_numbers=0,
            unsupported_numbers=[],
            issues=[
                # 6 个 code 是冻结的（`tests/test_contracts_d5.py` 逐个钉过），
                # 里面没有"校验自身失败"这一种。与其新造一个，不如用最接近的
                # `FAILED_TASK`，并在消息里说清是**校验这一步**出的问题。
                ValidationIssue(
                    severity="error",
                    code="FAILED_TASK",
                    message=(
                        f"校验过程本身出错了（{type(exc).__name__}），"
                        "这次分析的数字没有被核对过，请查服务日志。"
                    ),
                    task_id=None,
                )
            ],
            checked_at=datetime.now(timezone.utc),
        )


def _validate(state: WorkflowState) -> ValidationResult:
    """按顺序跑五类检查，把结果拼成 `ValidationResult`。"""
    checked, unsupported, number_issues = _check_numbers(state)
    issues: list[ValidationIssue] = []
    issues.extend(_check_tasks(state))
    issues.extend(_check_charts(state))
    issues.extend(number_issues)
    issues.extend(_check_columns(state))
    issues.extend(_check_report_length(state))
    return ValidationResult(
        passed=not any(issue.severity == "error" for issue in issues),
        checked_numbers=checked,
        unsupported_numbers=unsupported,
        issues=issues,
        checked_at=datetime.now(timezone.utc),
    )


def _check_tasks(state: WorkflowState) -> list[ValidationIssue]:
    """任务层面的问题：失败、被跳过、成功但一行结果都没有。"""
    issues: list[ValidationIssue] = []
    if state.plan is not None:
        for task in state.plan.tasks:
            if task.status is TaskStatus.FAILED:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="FAILED_TASK",
                        message=f"任务 {task.task_id}（{task.description}）执行失败，报告里缺了这一部分结论。",
                        task_id=task.task_id,
                    )
                )
            elif task.status is TaskStatus.SKIPPED:
                # 被跳过本身不是新问题：它的根因是依赖的任务失败了，那条已经记了
                # error。这里记 warning 是为了让"这份报告少了一块"可见，
                # 而不是把同一个错误算两遍。
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="FAILED_TASK",
                        message=(
                            f"任务 {task.task_id}（{task.description}）因为依赖的任务失败被跳过了，"
                            "根因记在上面那条失败里。"
                        ),
                        task_id=task.task_id,
                    )
                )
    for result in state.results:
        if not result.ok:
            continue
        if _is_empty_result(result):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="EMPTY_RESULT",
                    message=(
                        f"任务 {result.task_id} 成功了，但一行结果都没返回"
                        f"（工具 {result.tool_name}）—— 这份数据在这条问题下确实没有结果。"
                    ),
                    task_id=result.task_id,
                )
            )
    return issues


def _is_empty_result(result: ToolResult) -> bool:
    """这次工具调用是不是"成功了但没结果"。

    两种表现都要认：工具报了 `row_count == 0`，或者 `rows` 是个空列表。
    只认其中一种的话，某个工具忘了填 `row_count` 就会漏掉。
    """
    data = result.data if isinstance(result.data, dict) else {}
    row_count = data.get("row_count")
    if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count == 0:
        return True
    rows = data.get("rows")
    return isinstance(rows, list) and not rows


def _check_charts(state: WorkflowState) -> list[ValidationIssue]:
    """图表层面的问题：登记的图找不到文件、找不到出处，以及"该有图却没有"。"""
    issues: list[ValidationIssue] = []
    charts_dir = config.get_settings().charts_path
    task_ids = {result.task_id for result in state.results}
    charted: set[str] = set()

    for chart in state.charts:
        charted.add(chart.source_task_id)
        if chart.source_task_id not in task_ids:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_CHART_FILE",
                    message=(
                        f"图表 {chart.filename} 记的是任务 {chart.source_task_id} 画的，"
                        "但这次分析里没有这个任务的结果，这张图的出处对不上。"
                    ),
                    task_id=chart.source_task_id,
                )
            )
        path = charts_dir / state.workflow_id / chart.filename
        if not path.is_file():
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_CHART_FILE",
                    message=(
                        f"图表 {chart.filename} 在结果里登记了，但磁盘上没有这个文件。"
                        "前端拿到这个 URL 只会显示一张裂图，比「这次没有图」更难查。"
                    ),
                    task_id=chart.source_task_id,
                )
            )

    # 闭环检查：任务要求出图、任务本身成功了，却没有登记对应的图 —— 那一定是
    # "出图"这一步失败了（Orchestrator 出图失败时不登记半个对象，见 §8.2.2）。
    # 出图失败时 `state.validation` 还不存在，没别的地方能记这件事，所以在这里补。
    if state.plan is not None:
        for task in state.plan.tasks:
            if task.chart is None or task.status is not TaskStatus.SUCCESS:
                continue
            if task.task_id not in charted:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="MISSING_CHART_FILE",
                        message=(
                            f"任务 {task.task_id}（{task.description}）要求出图，但这次分析里没有它的图 —— "
                            "出图那一步失败了，具体原因在服务日志里。"
                        ),
                        task_id=task.task_id,
                    )
                )
    return issues


def _check_numbers(state: WorkflowState) -> tuple[int, list[str], list[ValidationIssue]]:
    """核心检查：报告正文里每个数字，都要能在工具结果里找到出处。

    返回 `(核对过的数字个数, 找不到出处的原始写法, 问题列表)`。
    """
    report = state.report
    if report is None:
        # 报告压根没生成，没有数字可核对。这件事由 `_check_report_length` 记，
        # 这里不重复报。
        return 0, [], []

    allowed = _allowed_numbers(state.results)
    checked = 0
    unsupported: list[str] = []
    issues: list[ValidationIssue] = []

    for section in report.sections:
        text = section.text or ""
        for raw in _NUMBER_RE.findall(text):
            value = _to_float(raw)
            if value is None:
                continue
            checked += 1
            if _is_supported(value, allowed):
                continue
            if raw not in unsupported:
                unsupported.append(raw)
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="UNSUPPORTED_NUMBER",
                    message=(
                        f"报告里出现了 {raw}，但所有工具结果里都找不到这个数字的出处。"
                        f"这一段是：「{_clip(text, 60)}」"
                    ),
                    task_id=section.cites[0] if section.cites else None,
                )
            )
    return checked, unsupported, issues


def _allowed_numbers(results: list[ToolResult]) -> set[float]:
    """把"允许出现在报告里的数字"收集成一个集合。

    三类来源，缺一不可：
    1. `data` 里的每个数值（工具算出来的结果本身）；
    2. 字符串里嵌着的数字（期间标签 `2024-01`）；
    3. **结构计数** —— 每个 list/dict 的长度，以及"某个键有值的条数"。
       报告里的"共 6 行结果"= `len(rows)`、"共 5 列有缺失值"= `null_count` 有值的
       列数，都是这一类。它们不是算出来的，但也没有别的出处。
    """
    allowed: set[float] = set()
    for result in results:
        _collect_numbers(result.data, allowed)
    return allowed


def _collect_numbers(node: Any, allowed: set[float]) -> None:
    """递归收集 `node` 里所有可以当作"数字出处"的值。"""
    # bool 是 int 的子类，必须排在前面挡掉：不然报告里写"是/否"会变成
    # 数字 1/0 的出处，等于凭空给编造的 1 开了后门。
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        allowed.add(float(node))
        return
    if isinstance(node, str):
        for text in _EMBEDDED_NUMBER_RE.findall(node):
            allowed.add(float(text))
        return
    if isinstance(node, dict):
        allowed.add(float(len(node)))
        for value in node.values():
            _collect_numbers(value, allowed)
        return
    if isinstance(node, list):
        allowed.add(float(len(node)))
        for item in node:
            _collect_numbers(item, allowed)
        # "某个键有值的条数"：`missing_values` 的 columns 是 9 个 dict，
        # 其中 null_count 有值的 5 个 —— 报告里的"共 5 列有缺失值"就是这个 5。
        if node and all(isinstance(item, dict) for item in node):
            for key in node[0]:
                allowed.add(float(sum(1 for item in node if item.get(key))))
        return


def _to_float(raw: str) -> float | None:
    """把抽出来的字符串读成 float。带千分位的 `24,871,657.00` 也认。"""
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _is_supported(value: float, allowed: set[float]) -> bool:
    """报告里的这个数字，在允许集里找得到出处吗。"""
    return any(_same_number(value, candidate) for candidate in allowed)


def _same_number(report_value: float, data_value: float) -> bool:
    """两个数算不算"同一个事实的两种写法"。

    三种写法都认（对应架构文档 §3.5.1 的容差规则）：
    1. 绝对值相等 —— 正负号不同也算，因为工具可能返回负数而报告写的是幅度；
    2. 四舍五入到 0/1/2 位小数后相等 —— `_num` 写两位小数、`_percent` 写一位小数；
    3. 百分比与小数互换 —— `_percent` 把 `0.0111` 写成 `1.1%`，这两个是同一个数。
    """
    if report_value == data_value or abs(report_value) == abs(data_value):
        return True
    for digits in (0, 1, 2):
        if round(abs(report_value), digits) == round(abs(data_value), digits):
            return True
    for scaled in (data_value * 100, data_value / 100):
        if report_value == scaled or abs(report_value) == abs(scaled):
            return True
        for digits in (0, 1, 2):
            if round(abs(report_value), digits) == round(abs(scaled), digits):
                return True
    return False


def _check_columns(state: WorkflowState) -> list[ValidationIssue]:
    """画像一致性：报告里点名的列，必须是真实存在的列。

    这里只认模板自己会写的那两种句式（"X 的均值是…"、"X = 值"）。
    抓不到的自由行文交给 `UNSUPPORTED_NUMBER` 兜底 —— 编造的列名后面通常
    紧跟着编造的数字，数字那关会拦下它。硬要去解析任意中文句子，
    只会换来一堆误报。
    """
    report = state.report
    if report is None:
        return []
    known = _known_names(state)
    if not known:
        return []

    issues: list[ValidationIssue] = []
    seen: set[str] = set()
    for section in report.sections:
        text = section.text or ""
        candidates = _COLUMN_STAT_RE.findall(text) + _COLUMN_ASSIGN_RE.findall(text)
        for name in candidates:
            name = name.strip()
            if not name or name in seen or name in known:
                continue
            seen.add(name)
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="UNKNOWN_COLUMN",
                    message=f"报告里提到了「{name}」，但这份数据的列里没有它（真实列名见数据画像）。",
                    task_id=section.cites[0] if section.cites else None,
                )
            )
    return issues


def _known_names(state: WorkflowState) -> set[str]:
    """所有"真实存在"的列名：画像里的列、工具报告的输出列、结果行的键。

    聚合列（`销售额_sum`）也在里面 —— 它是工具真实拼出来的列名，
    不是编造的。少了它，每一份分组汇总的报告都会被误报一次。
    """
    names: set[str] = set()
    profile = state.profile
    if profile is not None:
        for column in profile.columns:
            names.add(column.name)
            for suffix in _AGG_SUFFIXES:
                names.add(f"{column.name}_{suffix}")
    for result in state.results:
        names.update(str(name) for name in result.columns)
        data = result.data if isinstance(result.data, dict) else {}
        for key in ("rows", "stats", "pairs"):
            for row in _as_dict_list(data.get(key)):
                names.update(str(name) for name in row)
    return names


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """只挑出 list 里的 dict，别的元素直接忽略（工具结果的形状由工具保证，
    校验层不做类型断言，读到什么算什么）。"""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _check_report_length(state: WorkflowState) -> list[ValidationIssue]:
    """报告是不是"短得像没写完"。"""
    report = state.report
    if report is None:
        return [
            ValidationIssue(
                severity="error",
                code="SHORT_REPORT",
                message="这次分析没有生成报告，没有任何结论可以核对。",
                task_id=None,
            )
        ]
    total = len(report.summary or "") + sum(len(section.text or "") for section in report.sections)
    if total < _MIN_REPORT_CHARS:
        return [
            ValidationIssue(
                severity="warning",
                code="SHORT_REPORT",
                message=(
                    f"报告正文只有 {total} 个字，短得像是没写完 —— "
                    "可能是报告那一步出了问题，服务日志里有记录。"
                ),
                task_id=None,
            )
        ]
    return []
