"""让大模型把**已经算好的数字**写成中文报告正文（D5 的报告重写路径）。

## 它解决什么问题

模板报告（`validation.py` 的 `_template_report`）是把工具结果翻译成一句话，读起来像
数据播报：数字一个不错，但没人替你解释"这说明什么"。这个模块让模型读**已经算好的数字**、
写 3–5 段中文结论 —— 这正是本项目的卖点：**模型只做解释，算数永远是 pandas 的事**。

## 三条边界（写错了会静默地变坏，所以写在文件顶部）

1. **模型看不见原始数据**：prompt 里只有 `state.results` 里已经算好的数字，以及画像里的
   **列名清单**（只有名字，没有任何统计量）。原始 DataFrame 一个值都不给（§3.4 硬规矩）——
   一旦给了，模型就可能"顺手自己算一个"，而那个数字没有任何工具出处。
   画像里的 `mean_value` / `null_count` 这类统计量**也刻意不给**：它们不在 `state.results` 里，
   模型一旦引用就会被 `validate()` 判成编造（`UNSUPPORTED_NUMBER`）—— 那不是模型的错，
   是我们给了它一个不能引用的数字。
2. **模型只写正文，不写别的东西**：`summary`、`data_quality_notes`、表格全部由
   `validation.py` 生成。原因是 `validate()` 的数字溯源**只扫 `sections[].text`**
   （`_check_numbers` 的扫描范围就是这个字段）。所以这里只把模型的话放进 `sections[].text`，
   让"模型写的每一个字都落在被扫描的范围里"；模型写进 `summary` 的数字根本不会被校验，
   那才是真正危险的缝。
3. **模型写坏就当没写**：不是 JSON、缺字段、`cites` 指不到任务、太短、长到失控 —— 任何一条
   都返回 `None`，由调用方退回模板报告。**绝不修改模型的文字**（见下）。

## 为什么不"修剪"模型的文字

看起来更省事的做法是"太长就截断、缺 `cites` 就补一个"。这里**故意不做**：

- 截断可能把一个数字切成两半（`12,463.92` → `12,463.`），而那半截数字在工具结果里找不到
  出处 —— 报告会带着一个"编造的数字"被校验拦下，还没人知道是截断造成的。
- 补 `cites` 等于替模型编一个"这段话有依据"的声明，正是本项目最不该做的事。

所以规则只有一条：**要么整段照用，要么整份退回模板**。

## 一次重写机会（§5.3 第 6 行 / §3.5.2）

`write(state, validation=None)` 被调用两次是**设计好的**：

- 第一次 `validation=None`：正常写一版；
- 第二次带着 `ValidationResult`：说明第一次写的数字里有找不到出处的，把**上一版正文**
  和"哪些数字没出处"一起给模型，让它改一版。只给一次，再不过就保留原文（状态降级为
  `SUCCESS_WITH_WARNINGS`，由 Orchestrator 的 `_final_status` 决定）。

"要不要进第二次"由 `validation.should_rewrite_report(state)` 判断 —— Orchestrator 不许碰
`app.llm`（有一条 AST 测试盯着），所以那个判断不能写在它里面。
"""

import json
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

# 从 `validation.py` 借用"报告形状"的那几个工具函数（含下划线开头的私有函数）。
# 为什么直接借用而不是各写一份：报告长什么样（首段是诚实标记、表格最多 20 行、
# 数据质量说明来自画像、标题怎么裁）**必须只有一处定义** —— 抄一份的结果是
# "模型路径的报告格式和模板路径慢慢长得不一样"，而这两条路最终要过同一套校验、
# 上同一个前端。import 写在模块顶层是安全的：`validation.py` 只在**函数内部**
# import 本模块，不成环。
from app.agents.validation import (
    _MAX_TABLE_ROWS,
    _MIN_REPORT_CHARS,
    _clip,
    _copy_table,
    _latest_results,
    _llm_used,
    _quality_notes,
    _summary,
)

logger = logging.getLogger(__name__)

# 报告段数上限。§3.5.2 写的是"3–5 段"：少于 3 段解释不透，多于 5 段变成流水账。
_MAX_SECTIONS = 5

# 单段正文的长度上限。正常一段是 1–3 句话（100–200 字），600 字已经是很啰嗦的模型了。
# 超了不是截断而是**整份退回模板**，理由见模块 docstring。
_MAX_SECTION_CHARS = 600

# 小标题的长度上限。`ReportSection.heading` 在契约里没有长度约束，
# 但前端把它渲染成一行小标题，太长的标题会把版式撑坏。
_MAX_HEADING_CHARS = 60

# prompt 里每个列表最多给多少项。直接复用报告表格的 20 行上限：
# 模型看到的明细和用户在网页上看到的表格一样多，"它数了 24 行我只看得到 20 行"这种
# 对不上的情况就不会发生。`row_count` 这类字段仍然是完整的（它们不是列表）。
_MAX_LIST_ITEMS = _MAX_TABLE_ROWS

# 重写提示词里最多列几条问题。问题清单可能很长（每个编造的数字一条），
# 全贴进去会把 prompt 撑大，而前几条已经足够让模型看出"我写错了哪类数字"。
_MAX_PROBLEMS = 5

# 系统提示词。写成模块级常量而不是函数里的字符串：它是"给模型看的接口"，
# 会被反复对照着改，放在文件顶部一眼能看到全貌（和 `app/llm/prompts.py` 同一个理由）。
REPORT_SYSTEM_PROMPT = """你是一个数据分析报告撰写助手。系统已经用 pandas 把数据算完了，
下面会把**已经算好的数字**给你。你的任务只有一个：把这些数字写成中文解释。

四条硬规矩（违反任何一条，系统会把你写的数字打回重写）：
1. 你写的每一个数字都必须**原样来自**给你的工具结果，不许自己算：不许求和、不许求平均、
   不许把两个数相减、不许凭常识补一个"大约"的数字。
2. 唯一允许的两种改写是：把 0 到 1 之间的小数写成百分比（0.0079 写成 0.8%）、
   以及四舍五入到最多两位小数。除此之外不要做任何算术。
3. 列名只能用工具结果或列名清单里出现过的名字，不许新造列名。
4. 不要写代码、不要写 SQL、不要输出 Python 表达式。

输出格式：只输出一个 JSON 对象，不要加解释文字，不要用 Markdown 代码块包起来：
{"sections": [{"heading": "小标题", "text": "这一段的正文", "cites": ["T1"]}]}

- 写 3 到 5 段。
- 每段都必须有 `cites`，里面写清这一段用了哪几个任务的结果（任务编号见给你的数据）。
- 规模数字（一共几行、几列、几对）用结果里现成的 `row_count`、`group_count`、
  `duplicate_row_count` 这类字段，不要自己数列表长度 —— 长列表最多只会给你 20 项。
- 不要写表格：表格由系统直接复制工具结果，不经过你。"""

# 重写轮的提示词。贴回模型上一版正文（见 `build_messages`）之后再追加这一条。
_REWRITE_NOTE_TEMPLATE = """你上一版报告里的这些数字，在工具结果里找不到出处，系统的数字校验没有通过：

{problems}

请重写一版报告。**不要再写这些数字**：要么换成工具结果里真实存在的数字，
要么把包含它们的整句话删掉。其余内容保持不变，输出格式与上一次相同。"""


def write(state: WorkflowState, validation: ValidationResult | None = None) -> FinalReport | None:
    """让模型写一版报告；返回 `None` 表示"这次没成，请用模板"。**恒不抛异常。**

    返回值是 `FinalReport` 而不是"几段文字"，因为报告的**形状**（标题、首段诚实标记、
    表格、数据质量说明）是 `validation.py` 定好的，这里只是把正文换成模型写的那几段。
    `None` 与"报告生成失败"是两件事：`None` 是"模型这条路今天不走"，模板照样有报告。

    `validation` 不为 None 时是**重写轮**：说明上一版有数字被校验拦下了（见模块 docstring）。
    """
    # import 写在函数里，理由和 `planner._ask_model` 一样：`app.llm.client` 会 import openai，
    # 放在模块顶层的话，"openai 装不上/版本不对"会让这个模块整体 import 失败 ——
    # 而模板报告这条路（完全不碰模型）不该跟着一起挂掉。
    from app.llm import client

    try:
        reply = client.chat(build_messages(state, validation))
    except Exception:
        # 网络超时、429、401、没配 key……全部是"模型这条路今天走不通"，
        # 不是"分析失败"：调用方会退回模板报告，用户照样拿到结果（§5.4 第 3 条）。
        logger.exception("模型撰写报告失败（调用阶段），改用模板报告")
        return None

    sections = _parse_sections(reply.content or "", state)
    if sections is None:
        return None
    return _assemble(state, sections)


def build_messages(
    state: WorkflowState, validation: ValidationResult | None = None
) -> list[dict[str, Any]]:
    """拼出给模型的 `messages`。

    为什么单独一个函数：它是"模型能看见什么"的**唯一**入口，测试要能直接检查它
    （"原始数据有没有漏进去"这件事只能在这里断言）。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {"role": "user", "content": _user_content(state)},
    ]

    if validation is not None:
        # 重写轮：把**上一版正文**原样贴回去，再追加一条"哪些数字没出处"的 user 消息。
        # 只贴 `sections` 不贴 `summary`：`summary` 是模板写的、这一轮也不会改它，
        # 贴进去会让模型以为首段也归它写。
        previous = _previous_sections_text(state)
        if previous:
            messages.append({"role": "assistant", "content": previous})
        messages.append({"role": "user", "content": _rewrite_note(validation)})

    return messages


def _user_content(state: WorkflowState) -> str:
    """用户消息：问题 + 工具结果 + 列名清单。**这里是模型唯一的数据来源。**"""
    payload = _result_payload(state)
    names = [column.name for column in (state.profile.columns if state.profile else [])]

    lines = [
        f"用户的问题：{state.question}",
        "",
        "下面是这次分析已经算好的工具结果（这是你唯一可以引用的数据来源）：",
        json.dumps(payload, ensure_ascii=False, indent=2),
    ]
    if names:
        lines += [
            "",
            f"数据集的列名（只能用这些名字）：{'、'.join(names)}",
        ]
    return "\n".join(lines)


def _result_payload(state: WorkflowState) -> list[dict[str, Any]]:
    """把成功的工具结果整理成给模型看的清单。

    只给**成功**的结果：失败的任务没有数字可写，把它们也塞进去，模型可能会试图
    "解释"一个失败的任务，而报告里已经有专门的段落说这件事（`_summary`）。
    """
    tasks = {task.task_id: task for task in (state.plan.tasks if state.plan else [])}
    payload: list[dict[str, Any]] = []

    for result in _latest_results(state):
        if not result.ok:
            continue
        task = tasks.get(result.task_id)
        payload.append(
            {
                "task_id": result.task_id,
                "tool": result.tool_name,
                "what_it_did": task.description if task is not None else "",
                "data": _shrink(result.data),
            }
        )

    return payload


def _shrink(value: Any) -> Any:
    """递归把长列表截到 `_MAX_LIST_ITEMS` 项；别的原样返回。

    为什么要截：`group_aggregate` 最多能返回 100 行、`time_series_summary` 是逐月一行，
    原样塞进 prompt 会让每次提问的 token 花费随数据量一起涨。
    截断**只动列表**，不动数字字段 —— `row_count` 这类"一共多少"的量必须完整，
    否则模型只能数它看到的 20 行，写出来的"共 20 行"反而是错的。
    """
    if isinstance(value, list):
        return [_shrink(item) for item in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return {str(key): _shrink(item) for key, item in value.items()}
    return value


def _previous_sections_text(state: WorkflowState) -> str:
    """把上一版报告的正文还原成模型能认的 JSON（重写轮的输入）。"""
    report = state.report
    if report is None:
        return ""
    payload = {
        "sections": [
            {"heading": section.heading, "text": section.text, "cites": list(section.cites)}
            for section in report.sections
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _rewrite_note(validation: ValidationResult) -> str:
    """把校验发现的问题整理成一条给模型的提示词。

    直接用 `issue.message`（校验自己写的原话，里面带着出问题的那个数字和它所在的句子），
    不在这里重新组织语言 —— 组织一遍就多一个"两处说法不一致"的机会。
    """
    problems = [
        issue.message
        for issue in validation.issues
        if issue.code == "UNSUPPORTED_NUMBER" and issue.severity == "error"
    ]
    listed = "\n".join(f"- {text}" for text in problems[:_MAX_PROBLEMS])
    return _REWRITE_NOTE_TEMPLATE.format(problems=listed or "- （校验没有给出具体条目）")


def _parse_sections(content: str, state: WorkflowState) -> list[ReportSection] | None:
    """把模型输出解析成报告段落；**任何一处不合格就返回 `None`**（整份退回模板）。

    三种处理方式，区别是有意的：

    - **整份退回**：不是 JSON、`sections` 不是非空列表、某段太长、总字数不够 ——
      这些是"这一版根本不能用"，用模板比用一个残缺的版本好。
    - **丢掉这一段**：缺标题、缺正文、`cites` 为空或指向不存在的任务 ——
      剩下的段落仍然是"每段都能指回任务"的合格报告。
    - **不接受就不改**：不做截断、不补 `cites`、不改数字（模块 docstring 有理由）。
    """
    data = _parse_json_object(content)
    if data is None:
        logger.warning("模型写的报告不是合法 JSON，改用模板报告")
        return None

    raw = data.get("sections")
    if not isinstance(raw, list) or not raw:
        logger.warning("模型写的报告里没有 sections 列表，改用模板报告")
        return None

    known_ids = {result.task_id for result in _latest_results(state) if result.ok}
    sections: list[ReportSection] = []

    for item in raw[:_MAX_SECTIONS]:
        section = _section_from(item, known_ids)
        if section is _TOO_LONG:
            logger.warning("模型写的某一段超过 %s 字，改用模板报告", _MAX_SECTION_CHARS)
            return None
        if section is not None:
            sections.append(section)

    if not sections:
        logger.warning("模型写的报告没有一段能用（缺标题/缺正文/cites 指不到任务），改用模板报告")
        return None

    total = sum(len(section.text) for section in sections)
    if total < _MIN_REPORT_CHARS:
        # 比 `validate()` 的"报告太短"阈值还短，就别拿它替换模板了：
        # 与其交一份注定被记 `SHORT_REPORT` 的报告，不如退回内容更全的模板。
        logger.warning("模型写的报告只有 %s 字，短于校验阈值，改用模板报告", total)
        return None

    return sections


# 一个哨兵对象，用来区分"这一段不能用（丢掉）"与"整份不能用（退回模板）"。
# 用 `None` 表示前者、用一个唯一对象表示后者，比返回元组或抛异常都更直白。
_TOO_LONG = object()


def _section_from(item: Any, known_ids: set[str]) -> ReportSection | object | None:
    """把模型给的**一段**变成 `ReportSection`；不合格返回 `None`，太长返回 `_TOO_LONG`。"""
    if not isinstance(item, dict):
        return None

    heading = item.get("heading")
    text = item.get("text")
    cites = item.get("cites")

    if not isinstance(heading, str) or not heading.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(cites, list) or not cites:
        # 没有 `cites` 的段落是"不知道这段数字是哪来的"—— 报告的底线是每段都能指回任务。
        return None

    text = text.strip()
    if len(text) > _MAX_SECTION_CHARS:
        return _TOO_LONG

    valid_cites = [str(task_id) for task_id in cites if str(task_id) in known_ids]
    if not valid_cites:
        # `cites` 指不到任何真实任务：要么模型编了任务号，要么那段话没有依据。
        return None

    return ReportSection(
        heading=_clip(heading.strip(), _MAX_HEADING_CHARS),
        text=text,
        cites=valid_cites,
        # 表格**不由模型写**（它连表格字段都没有）：表格必须是工具结果的直接复制
        # （§13 风险 4 ③）。这里先留空，由 `_assemble` 按 `cites` 挂上去。
        table=None,
    )


def _assemble(state: WorkflowState, sections: list[ReportSection]) -> FinalReport:
    """把模型写的几段拼成一份完整报告：形状与模板路径完全一致，只有正文是模型的。"""
    by_task = {result.task_id: result for result in _latest_results(state)}
    for section in sections:
        # 只有 `cites` 恰好指向**一个**任务时才挂表格：指向多个任务时，
        # 无法诚实地回答"这张表是哪个工具算的"，宁可不挂。
        if len(section.cites) == 1:
            result = by_task.get(section.cites[0])
            if result is not None and result.ok:
                section.table = _copy_table(result.data)

    # `citations` 是"这次成功产出了结果的任务"，不是"模型引用了哪些任务"——
    # `_summary` 用它数"成功 N 个"，前端也拿它做核对。模型的引用写在每段的 `cites` 里。
    citations = [result.task_id for result in _latest_results(state) if result.ok]

    return FinalReport(
        report_id=uuid4().hex[:8],
        title=f"分析报告：{_clip(state.question, 60)}",
        # `model_wrote=True` 让首段换成第三句话。这是"哪一步用了模型"的诚实标记：
        # 这句话必须逐字为真，否则就变成 D-22 记的那类"数字对、话是假的"。
        summary=_summary(state, citations, model_wrote=True),
        sections=sections,
        data_quality_notes=_quality_notes(state),
        citations=citations,
        generated_at=datetime.now(timezone.utc),
        # `llm_used` 的口径不变：仍按 `plan.source` 推导（D4 QA 重要问题 2 的守卫）。
        # 这里借 `validation._llm_used` 而不是自己写 `True`：`report_llm.write` 只在
        # `should_try_llm_report` 为真时才会被调用（那一条已经要求
        # `plan.source == "llm"`），但"口径只有一处定义"比"这次正好相等"重要 ——
        # 抄一份 True 的结果是以后改口径时两条路慢慢不一致。
        llm_used=_llm_used(state),
    )


def _parse_json_object(content: str) -> dict[str, Any] | None:
    """从模型输出里抠出一个 JSON 对象；抠不出来返回 `None`。

    两层容错，和 `planner._parse_arguments` 同一个思路：模型很爱在 JSON 前后加一句
    "好的，这是报告"或者包一层 ```json 代码块，所以第一层直解失败后，用正则抠出
    **第一个 `{` 到最后一个 `}`** 再试一次（贪婪匹配正好覆盖"前后各包一层"的情况）。
    """
    text = (content or "").strip()
    if not text:
        return None

    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None

    if isinstance(value, dict):
        return value

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None

    try:
        value = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None

    return value if isinstance(value, dict) else None
