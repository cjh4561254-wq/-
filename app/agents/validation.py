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
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.schemas_workflow import (
    FinalReport,
    ReportSection,
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
    """正常路径：每个成功的任务一段，外加数据质量说明。"""
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
            sentence += f"，另有 {len(with_nulls) - 3} 列也存在缺失"

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


def _summary(state: WorkflowState, citations: list[str]) -> str:
    """总述：第一段是**逐字**的诚实标记，第二段是这次跑成了什么。

    第一段用哪句取决于"模型到底参与了哪一步"（`_llm_used` 的口径）：
    全程没模型 → TEMPLATE_NOTICE；模型参与了规划 → LLM_PLANNED_NOTICE。
    两句话都是常量，因为**这句话是要被测试逐字断言的**（验收标准 ① ② 都钉了它）。
    """
    tasks = state.plan.tasks if state.plan else []
    failed = [task for task in tasks if task.status.value == "FAILED"]
    skipped = [task for task in tasks if task.status.value == "SKIPPED"]

    lines = [LLM_PLANNED_NOTICE if _llm_used(state) else TEMPLATE_NOTICE]
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
