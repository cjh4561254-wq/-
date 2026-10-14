"""规则兜底计划器：把中文问题变成一份**可执行**的任务计划，零 LLM 参与。

## 为什么要有它（不是"临时占位"）

系统有两种规划方式：模型规划（D4 加）和这里的规则规划。规则兜底存在的理由有三条，
每一条都是真实的工程需求，而不是"以后可能会用"：

1. **没有 API Key 也能演示**：第 4 天要演示时密钥可能还没配好（架构文档 §11 风险 6）。
   `llm_configured=false` 时系统照常工作，`source="rule_fallback"` 如实记录。
2. **模型输出不稳定**：同一个问题两次返回，一次合法 JSON 一次带解释性前后缀，
   或者干脆用了不存在的工具名。规则计划是**永远可用**的那条路（§11 风险 1）。
3. **计划本身要"可执行"**：这里产出的每个任务都会真的进 `run_task` 去算。
   所以列名必须来自画像、参数必须过得了 5 类校验 —— 计划写错一个列名，
   用户看到的就是一个失败的任务。

## 选列的两层逻辑（Lead 已拍板的口径）

每条规则都要回答"用哪一列"。做法固定为两层：

1. **问题里提到的真实列名优先**：用户说"销售额"就该算 `销售额`，这是用户的原意。
2. **问题里没提，才退回画像里的默认列**（第一个符合语义类型的列）。

两层都只在 `profile.columns` 里挑，所以**永远不会编造列名**——
"按地区分析"这句话里如果 `地区` 不是真实列名，规则直接跳过，
而不是硬塞一个 `地区` 进去让执行阶段去报 `COLUMN_NOT_FOUND`。
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from app import config
from app.schemas_dataset import DatasetProfile
from app.schemas_workflow import AnalysisTask, ChartRequest, TaskPlan

logger = logging.getLogger(__name__)

# TaskPlan.tasks 的硬上限（契约里是 min_length=1 / max_length=8）。
# `max_steps` 是请求侧的旋钮，但绝不能让它把计划顶破契约：传 0 会得到空计划、
# 传 20 会得到 9 个任务，两种都会让 TaskPlan 直接抛 ValidationError。
# 这里把它夹到 1..8，"绝不允许空计划"才是一句有代码保证的话。
_MIN_TASKS = 1
_MAX_TASKS = 8

# 一条都没命中时的兜底任务：至少给用户一份描述性统计，
# 而不是回一句"我不知道你想问什么"——那是产品失败，不是数据失败。
_FALLBACK_DESCRIPTION = "对每个数值列做描述性统计（计数、均值、分位数、最大最小值）"


# ---------------------------------------------------------------------------
# 出图请求（D5）：计划阶段就决定"这个任务要不要配图、画哪两列"
# ---------------------------------------------------------------------------

# 为什么出图这件事要在**计划阶段**定下来，而不是执行完再猜：
# `AnalysisTask.chart` 是契约里的字段，执行阶段的 `run_task` 只认工具、不认图；
# 图由编排器（charting 阶段）拿着"任务 + 工具结果"去画。所以"画哪两列"必须在
# 任务被创建时就写清楚 —— 到了 charting 阶段再回头看结果去猜，猜错了没人能发现。
#
# 哪些工具配图：**只给"结果里有 rows"的工具配**。这不是"顺手多给几个"，
# 而是三类工具各自的形状决定的：
#   - 有 rows（group_aggregate / top_n / compare_groups / time_series_summary）→ 能画
#   - describe_dataset 的 stats 是"每列一行统计量"，画出来是 9 根含义不同的柱子，
#     读者会以为在比较列与列的大小 —— 不配图才是对的
#   - missing_values 的 columns 是"每列的缺失个数"，形状上能画，但 9 列里绝大多数是 0，
#     图上只有一两根柱子立着，看不出信息
#   - correlation 只有 pairs（哪两列、一个系数），没有原始数据点，画不出散点图
_CHART_TYPE_BY_TOOL: dict[str, str] = {
    "group_aggregate": "bar",
    "top_n": "bar",
    "compare_groups": "bar",
    "time_series_summary": "line",
}

# `ChartRequest.title` 有 `max_length=40` 的契约限制。标题是给人看的，
# 超出就截断并加省略号 —— 让 TaskPlan 构造失败（因为标题长了 2 个字）
# 是拿契约去惩罚用户，代价完全不成比例。
_MAX_TITLE_CHARS = 40


def _chart_for(tool_name: str, params: dict[str, Any], title: str) -> ChartRequest | None:
    """给一个任务配出图请求；这个工具不配图时返回 `None`。

    `x_field` / `y_field` 必须是**工具结果里真实存在的列名**，所以它们是从
    `params` 按各工具的列名推导规则算出来的，不是猜的：

    - `group_aggregate`：结果列是"分组列 + `{metric}_{agg}`"（见 `app/tools/aggregate.py`）
    - `compare_groups`：结果列是"分组列 + `{metric}_{agg}` + 差值"
    - `time_series_summary`：结果列是"`period` + `{metric}_{agg}`（+ 环比）"
    - `top_n`：`columns` 不传时只返回"分组列 + 排序列"，所以 `x_field` 和 `y_field`
      都是 `by` 这一列（x 轴用行号当标签，见 `app/agents/charts.py`）

    参数缺了关键项时返回 `None` 而不是抛异常：计划阶段的任务本来就该是
    "能执行就执行"，配不出图顶多是少一张图，不该让整份计划失败。
    """
    chart_type = _CHART_TYPE_BY_TOOL.get(tool_name)
    if chart_type is None:
        return None

    if tool_name == "group_aggregate":
        group_by = params.get("group_by") or []
        metric = params.get("metric")
        # 不传 metric 时聚合的是行数，结果列名就是 `count`（aggregate.py 的命名规则）
        value_column = f"{metric}_{params.get('agg', 'sum')}" if metric else "count"
        if not group_by:
            return None
        x_field, y_field = str(group_by[0]), value_column
    elif tool_name == "compare_groups":
        x_field = params.get("group_by")
        metric = params.get("metric")
        if not x_field or not metric:
            return None
        x_field = str(x_field)
        y_field = f"{metric}_{params.get('agg', 'mean')}"
    elif tool_name == "time_series_summary":
        metric = params.get("metric")
        if not metric:
            return None
        x_field, y_field = "period", f"{metric}_{params.get('agg', 'sum')}"
    else:  # top_n：结果里只有 by 这一列有数值
        by = params.get("by")
        if not by:
            return None
        x_field = y_field = str(by)

    return ChartRequest(
        chart_type=chart_type,
        x_field=x_field,
        y_field=y_field,
        title=_clip_title(title),
    )


def _clip_title(title: str) -> str:
    """把标题夹到契约允许的长度内。空标题给一个中性说法，避免前端显示空白。"""
    text = (title or "").strip() or "分析结果"
    if len(text) <= _MAX_TITLE_CHARS:
        return text
    return text[: _MAX_TITLE_CHARS - 1] + "…"


@dataclass(frozen=True)
class _Rule:
    """一条关键词规则：命中哪些词、生成哪个工具的任务、参数怎么填。

    `build` 返回 `(params, description)`；返回 `None` 表示"这条规则命中了关键词，
    但画像里没有它需要的列"——调用方会跳过它（不编造列名）。
    """

    label: str
    keywords: tuple[str, ...]
    tool_name: str
    kind: str
    build: Callable[[str, DatasetProfile], tuple[dict[str, Any], str] | None]


def plan_with_rules(question: str, profile: DatasetProfile, max_steps: int = 6) -> TaskPlan:
    """用关键词规则生成计划。`source` 恒为 `"rule_fallback"`。

    参数：
        question:  用户原问题（中文）。原样存进 `TaskPlan.question`，便于复盘。
        profile:   数据画像。**所有列名都从这里来**，函数不读文件、不碰数据本身。
        max_steps: 最多生成几个任务（请求侧旋钮，会被夹到 1..8）。

    规则按优先级从高到低依次尝试（顺序就是 `_RULES` 的顺序）：
    命中就生成任务、同时命中多条就生成多个任务，总数不超过 `max_steps`。
    """
    limit = max(_MIN_TASKS, min(int(max_steps), _MAX_TASKS))

    tasks: list[AnalysisTask] = []
    hit_labels: list[str] = []

    for rule in _RULES:
        if len(tasks) >= limit:
            break  # 额度用完。不报错、也不丢弃已生成的任务，后面的规则这一轮不参与
        if not _mentions(question, rule.keywords):
            continue

        built = rule.build(question, profile)
        if built is None:
            # 关键词命中了，但画像里缺这条规则要用的列（例：问"趋势"却没有日期列）。
            # 跳过，不编造列名 —— 编出来的列名会让这个任务在执行阶段直接失败，
            # 而用户看到的会是"工具报错"，真正的原因却是"这个数据集做不了这种分析"。
            continue

        params, description = built
        hit_labels.append(rule.label)
        # task_id / order 在这里统一编号，规则本身不关心自己是第几个任务
        tasks.append(
            AnalysisTask(
                task_id=f"T{len(tasks) + 1}",
                order=len(tasks) + 1,
                kind=rule.kind,
                tool_name=rule.tool_name,
                params=params,
                description=description,
                chart=_chart_for(rule.tool_name, params, description),
            )
        )

    if not tasks:
        # 一个规则都没命中，或命中的规则全部因缺列被跳过。
        # 兜底：描述性统计对任何数据集都成立（只要它有一个数值列），保证计划非空。
        hit_labels.append("兜底")
        tasks.append(
            AnalysisTask(
                task_id="T1",
                order=1,
                kind="describe",
                tool_name="describe_dataset",
                params={},
                description=_FALLBACK_DESCRIPTION,
            )
        )

    return TaskPlan(
        plan_id=uuid4().hex[:8],
        question=question,
        dataset_id=profile.dataset_id,
        tasks=tasks,
        reasoning=f"命中规则：{'、'.join(hit_labels)}（规则兜底，未使用大模型）",
        source="rule_fallback",
        created_at=datetime.now(timezone.utc),
    )


def llm_is_configured() -> bool:
    """模型是否配置齐了（D4 用它决定"走模型规划还是直接走规则兜底"）。

    配置在**函数内**读，不在模块顶层读：模块顶层调 `get_settings()` 会把配置
    绑死在 import 那一刻，测试里改环境变量、`reset_settings_cache()` 都不再起作用
    （本项目真踩过这个坑，见 `docs/DECISIONS.md`）。`lru_cache` 保证它依然是廉价的。
    """
    return bool(config.get_settings().llm_configured)


# ---------------------------------------------------------------------------
# 模型规划（D4）：`plan()` —— 恒不抛异常，永远返回一个可执行的计划
# ---------------------------------------------------------------------------

# 工具名 → `AnalysisTask.kind`。`kind` 是给前端分类显示用的，必须是契约里那 6 个字面量。
# 写成 dict 而不是 if/elif：新增工具时只多一行，不会漏改某个分支。
_KIND_BY_TOOL: dict[str, str] = {
    "describe_dataset": "describe",
    "group_aggregate": "aggregate",
    "top_n": "aggregate",
    "missing_values": "missing",
    "correlation": "correlation",
    "compare_groups": "compare",
    "time_series_summary": "trend",
}

# 模型给的参数不是合法 JSON 时，最多再试几次（§5.3 的表：重试 2 次）。
# 第一次重试会把原文附回 Prompt —— 只说"你写错了"，模型无从知道错在哪。
_MAX_JSON_RETRIES = 2

# 模型没有给 `description` 时，按工具名生成的中文说明模板。
# 为什么不用模型自己写的描述：它可能写出"分析销售额"这种没有信息量的句子，
# 而报告的小标题直接用它（§5.4）。工具名 + 参数是确定的，生成出来的描述不会跑偏。
_DESCRIPTION_BY_TOOL: dict[str, str] = {
    "describe_dataset": "对数值列做描述性统计",
    "group_aggregate": "按{group_by}分组汇总{metric}",
    "top_n": "按{by}从大到小取前 {n} 行",
    "missing_values": "检查每一列的缺失值与重复行",
    "correlation": "计算数值列两两之间的相关系数",
    "compare_groups": "按{group_by}分组比较{metric}",
    "time_series_summary": "按月汇总{metric}的走势",
}


def plan(question: str, profile: DatasetProfile, max_steps: int = 6) -> TaskPlan:
    """制定分析计划：**优先让模型规划，失败就退回规则兜底**。恒不抛异常。

    为什么"恒不抛异常"是函数签名级别的保证，而不是"尽量别抛"
    ------------------------------------------------------
    架构文档 §13 风险 1 第 ⑤ 条把它定成验收项：**模型彻底失败时系统仍然可用**。
    所以兜底不能是"我们记得在每个分支里 catch 一下"，那迟早会漏一个分支
    （新增一种失败方式 → 忘了加 except → 整条工作流 FAILED）。
    这里用最外层一个 `try/except Exception` 兜住**所有**路径，
    包括"我们没预料到的"那类 —— 这才是"签名保证"。

    返回值：
        永远是一个 `TaskPlan`，`tasks` 至少 1 个。
        走模型路径时 `source="llm"`；任何一步失败时 `source="rule_fallback"`
        （规则兜底那条路的返回值原样来自 `plan_with_rules`，不做二次加工）。
    """
    limit = max(_MIN_TASKS, min(int(max_steps), _MAX_TASKS))

    try:
        if not llm_is_configured():
            # 没配模型是**常态**（第 4 天演示时密钥可能还没配好），不是错误，
            # 所以这里连 warning 都不打 —— 每次提问都刷一条日志会把真正的告警淹掉。
            return plan_with_rules(question, profile, max_steps)

        reply = _ask_model(question, profile)
        if reply is None:
            return plan_with_rules(question, profile, max_steps)

        tasks = _tasks_from_reply(reply, profile, limit)
        if not tasks:
            # 模型调了工具，但**没有一个任务能用**：工具名不在白名单、参数缺列、
            # 或者 JSON 一直不合法。丢弃它们是对的（§6.2 第 2 条：不猜一个相似的名字），
            # 但空计划会让 `TaskPlan` 直接抛 ValidationError，所以必须退回规则兜底。
            logger.warning(
                "模型返回的 %s 个工具调用全部不可用，改用规则兜底计划", len(reply.tool_calls)
            )
            return plan_with_rules(question, profile, max_steps)

        return TaskPlan(
            plan_id=uuid4().hex[:8],
            question=question,
            dataset_id=profile.dataset_id,
            tasks=tasks,
            reasoning=f"由大模型规划：{len(tasks)} 个任务（模型 {reply.model or '未知'}）",
            source="llm",
            created_at=datetime.now(timezone.utc),
        )

    except Exception:
        # 兜住一切：未配置、超时、网络错、密钥错、JSON 坏、我们自己的 bug……
        # 打完整堆栈（排查需要），然后交给规则兜底 —— 用户拿到的仍然是一份能跑的计划。
        logger.exception("模型规划失败，改用规则兜底计划")
        return plan_with_rules(question, profile, max_steps)


def _ask_model(question: str, profile: DatasetProfile):
    """问模型要工具调用，JSON 不合法时最多重试 `_MAX_JSON_RETRIES` 次。

    返回 `LLMReply`；`None` 表示"一直没拿到合法 JSON"。

    为什么 import 写在函数里（本文件唯一一处）
    ----------------------------------------
    `app.llm.client` 会 import openai。放在模块顶层的话，**openai 装不上或版本不对
    会让整个 planner 模块 import 失败** —— 连完全不碰模型的规则兜底一起挂掉，
    而那正是"模型不可用时系统仍然可用"要避免的情况。
    """
    from app.llm import client
    from app.llm.prompts import build_planner_messages, build_retry_note
    from app.tools import registry

    messages = build_planner_messages(question, profile)
    # 只把白名单里的 7 个工具给模型看：它在物理上就看不见别的东西（§6.2 第 1 条）。
    tools = registry.to_openai_tools()

    for attempt in range(_MAX_JSON_RETRIES + 1):
        reply = client.chat(messages, tools=tools)

        broken = _first_unparsable_arguments(reply)
        if broken is None:
            return reply

        if attempt >= _MAX_JSON_RETRIES:
            logger.warning("模型连续 %s 次给出无法解析的参数，放弃模型规划", attempt + 1)
            return reply

        logger.warning("模型给的参数不是合法 JSON，第 %s 次重试", attempt + 1)
        # 把**模型自己的那条 message 原样 append 回去**（含 reasoning_content，
        # 这是思考模式的硬要求，见 D-18），再追加一条指出错在哪的 user 消息。
        # 只改原文不 append 的话，模型看到的历史是"我问了什么"，它会重新规划一遍。
        messages = [
            *messages,
            reply.message,
            {"role": "user", "content": build_retry_note(broken)},
        ]

    return None


def _first_unparsable_arguments(reply) -> str | None:
    """找出第一个参数解析不了的工具调用，返回它的**原始字符串**；全都能解析则返回 None。"""
    for call in reply.tool_calls:
        if _parse_arguments(call.arguments) is None:
            return call.arguments
    return None


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    """把模型给的 `arguments` 字符串解析成 dict；解析不了返回 `None`。

    三层容错（§4.14.3）：
        ① `json.loads` 直接解析；
        ② 失败时用正则抽出**第一个 `{...}`** —— 模型很爱在 JSON 前后加一句
           "好的，我来调用工具"或者包一层 ```json 代码块，抠出来就能用；
        ③ 还不行返回 `None`，由调用方决定是重试还是丢弃。

    `re.S` 让 `.` 也能匹配换行：模型给的 JSON 常常是格式化的多行。
    `.*` 是**贪婪**的，所以它匹配到的是最后一个 `}` —— 这正好是我们要的：
    前后各包一层时，"从第一个 `{` 到最后一个 `}`"才是完整的 JSON。
    """
    text = (raw or "").strip()
    if not text:
        # 模型调了一个不需要参数的工具（如 `missing_values`），有些服务会回空串。
        # 空参数等同于 `{}`，不是错误。
        return {}

    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None

    if not isinstance(value, dict):
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                value = json.loads(match.group(0))
            except (ValueError, TypeError):
                value = None

    return value if isinstance(value, dict) else None


def _tasks_from_reply(reply, profile: DatasetProfile, limit: int) -> list[AnalysisTask]:
    """把模型的工具调用逐个变成 `AnalysisTask`，**不可用的直接丢弃**。

    三层检查，任何一层不过就丢这个任务（§6.2 第 2 条：不猜一个相似的工具名去凑）：
        ① 工具名必须 `registry.get(name) is not None`；
        ② `arguments` 必须是合法 JSON 对象；
        ③ 参数必须过得了"画像层面"的检查（必填项齐、不认识的参数、列名真实存在）。

    第 ③ 层为什么只查画像、不调 `validate_call`
    ------------------------------------------
    `validate_call` 需要 DataFrame（它要判断"这一列是不是数值列"），而这个函数
    的入参只有 `profile` —— 计划阶段根本不该去碰数据。所以这里只做"看画像就能判断"
    的那部分：**列名是否真实存在**。剩下的类型/范围/枚举由执行阶段的
    `validate_call` 兜住（那才是它的职责），两边合起来才是完整的 5 类校验。
    """
    from app.tools import registry

    tasks: list[AnalysisTask] = []

    for call in reply.tool_calls:
        if len(tasks) >= limit:
            break

        params = _parse_arguments(call.arguments)
        if params is None:
            logger.warning("丢弃任务：工具 %s 的参数不是合法 JSON", call.name)
            continue

        spec = registry.get(call.name)
        if spec is None:
            logger.warning("丢弃任务：工具 %s 不在白名单里", call.name)
            continue

        reason = _check_params_against_profile(spec, params, profile)
        if reason is not None:
            logger.warning("丢弃任务：工具 %s 的参数不可用（%s）", call.name, reason)
            continue

        description = _describe(call.name, params)
        tasks.append(
            AnalysisTask(
                task_id=f"T{len(tasks) + 1}",
                order=len(tasks) + 1,
                kind=_KIND_BY_TOOL.get(call.name, "describe"),
                tool_name=call.name,
                params=params,
                description=description,
                chart=_chart_for(call.name, params, description),
            )
        )

    return tasks


def _check_params_against_profile(spec, params: dict[str, Any], profile: DatasetProfile) -> str | None:
    """按 `params_schema` 检查参数；返回 `None` 表示可用，否则返回中文原因。"""
    schema = spec.params_schema
    properties = schema.get("properties", {})

    for name in schema.get("required", []):
        if name not in params:
            return f"缺少必填参数 {name}"

    known_columns = {column.name for column in profile.columns}

    for name, value in params.items():
        rule = properties.get(name)
        if rule is None:
            # 不认识的参数直接丢：执行阶段的 `validate_call` 也会拒它，
            # 但在计划阶段丢掉更好 —— 用户看到的会是"少了一个任务"，
            # 而不是"这个任务失败了"。
            return f"不认识的参数 {name}"
        if rule.get("column"):
            for column in _as_column_values(rule, value):
                if column not in known_columns:
                    # 这一条就是"不许编造列名"在模型路径上的落点。
                    return f"列 {column} 不在数据画像里"

    return None


def _as_column_values(rule: dict[str, Any], value: Any) -> list[Any]:
    """取出参数里"代表列名"的那些值：`list[str]` 参数展开，其余当成单值。"""
    if rule.get("type") == "list[str]":
        return list(value) if isinstance(value, list) else [value]
    return [value]


def _describe(tool_name: str, params: dict[str, Any]) -> str:
    """给任务生成一句中文说明（报告的小标题直接用它）。

    为什么不用模型写的描述：模型可能写出"分析数据"这种没有信息量的句子，
    也可能写出与实际参数不符的描述（参数是 `销量`、描述里写 `销售额`）。
    工具名 + 参数是**确定的**，由它生成的描述不会跑偏。
    模板里缺的占位符用 `.get(...)` 拿，拿不到就留空 —— 宁可少几个字，也不编。
    """
    template = _DESCRIPTION_BY_TOOL.get(tool_name, f"执行 {tool_name}")

    # 分组列在工具里是 `list[str]`（最多 3 列），描述里用"、"连起来更像中文
    group_by = params.get("group_by")
    if isinstance(group_by, list):
        group_by = "、".join(str(name) for name in group_by)

    try:
        return template.format(
            group_by=group_by or "",
            metric=params.get("metric") or "",
            by=params.get("by") or "",
            n=params.get("n", ""),
        ).strip()
    except (KeyError, IndexError):
        # 模板里的占位符拼错了（开发期 bug）。不能因此丢任务 ——
        # 退回一个朴素但正确的描述，并留一条日志。
        logger.warning("工具 %s 的描述模板有误，已退回通用描述", tool_name)
        return f"执行 {tool_name}"



# ---------------------------------------------------------------------------
# 规则表：顺序 = 优先级（从上往下，先命中的先生成任务）
# ---------------------------------------------------------------------------


def _build_missing_values(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """缺失值 / 重复行检查。不需要任何列名，所以永远能生成。"""
    return {}, "检查每一列的缺失值数量与占比，并统计完全重复的行"


def _build_correlation(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """相关系数。**至少 2 个数值列**才有意义：1 列算不出"关系"。"""
    if len(_columns_of_type(profile, ("numeric",))) < 2:
        return None
    return {}, "计算数值列两两之间的相关系数（看哪些指标同涨同跌）"


def _build_time_series(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """时间趋势。需要**一个日期列 + 一个数值列**，缺一个就跳过。"""
    date_column = _pick_column(question, profile, ("datetime",))
    metric = _pick_column(question, profile, ("numeric",))
    if date_column is None or metric is None:
        return None
    return (
        {
            "date_column": date_column,
            "metric": metric,
            "freq": "M",  # 演示场景按月最直观（D/W 会在图上挤成一团）
            "agg": "sum",
            "include_growth": True,  # 环比是"变化"这个问题真正想要的答案
        },
        f"按月汇总{metric}的走势，并算出环比变化",
    )


def _build_top_n(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """排行。需要一个数值列。`n` 固定 10 —— "前几/最高"这类问法在演示里就是看前十。"""
    by = _pick_column(question, profile, ("numeric",))
    if by is None:
        return None
    return {"by": by, "n": 10}, f"按{by}从大到小取前 10 行"


def _build_group_aggregate(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """分组汇总。需要一个分类列 + 一个数值列。

    `group_by` 必须是 `list[str]`（工具 schema 就是这么定的，最多 3 列）；
    这里只分一组 —— "各地区的销售额"要的是按地区，多分一列会让行数乘起来，
    演示时反而看不清。
    """
    group_by = _pick_column(question, profile, ("categorical",))
    metric = _pick_column(question, profile, ("numeric",))
    if group_by is None or metric is None:
        return None
    return (
        {
            "group_by": [group_by],
            "metric": metric,
            "agg": "sum",
            "sort_order": "desc",  # 从高到低，用户一眼看到最大的是谁
            "limit": 20,
        },
        f"按{group_by}分组汇总{metric}，从高到低取前 20 组",
    )


def _build_compare_groups(question: str, profile: DatasetProfile) -> tuple[dict[str, Any], str] | None:
    """分组比较。用**平均值**而不是求和：比较"哪个组更高"时，总和会被组内行数带偏。"""
    group_by = _pick_column(question, profile, ("categorical",))
    metric = _pick_column(question, profile, ("numeric",))
    if group_by is None or metric is None:
        return None
    return (
        {"group_by": group_by, "metric": metric, "agg": "mean"},
        f"按{group_by}分组比较{metric}的平均值",
    )


_RULES: list[_Rule] = [
    _Rule(
        label="缺失值检查",
        keywords=("缺失", "空值", "重复", "脏", "质量", "完整"),
        tool_name="missing_values",
        kind="missing",
        build=_build_missing_values,
    ),
    _Rule(
        label="相关性分析",
        keywords=("相关", "关系", "关联"),
        tool_name="correlation",
        kind="correlation",
        build=_build_correlation,
    ),
    _Rule(
        label="时间趋势",
        keywords=("趋势", "变化", "月度", "月份", "时间", "走势"),
        tool_name="time_series_summary",
        kind="trend",
        build=_build_time_series,
    ),
    _Rule(
        label="Top N 排行",
        keywords=("最高", "最多", "前几", "排行", "Top", "top", "最大"),
        tool_name="top_n",
        kind="aggregate",
        build=_build_top_n,
    ),
    _Rule(
        label="分组汇总",
        keywords=("各", "按", "分组", "分别", "不同", "每个"),
        tool_name="group_aggregate",
        kind="aggregate",
        build=_build_group_aggregate,
    ),
    _Rule(
        label="分组比较",
        keywords=("比较", "差异", "对比"),
        tool_name="compare_groups",
        kind="compare",
        build=_build_compare_groups,
    ),
]


# ---------------------------------------------------------------------------
# 选列与匹配
# ---------------------------------------------------------------------------


def _mentions(question: str, keywords: tuple[str, ...]) -> bool:
    """问题里有没有出现这些关键词（ASCII 关键词不区分大小写，中文本来就没有大小写）。

    同时查原文和小写副本：`"Top 10"` 与 `"top 10"` 都该命中，
    而中文关键词在两种写法下结果一样，不会因此多命中。
    """
    lowered = question.lower()
    return any(keyword in question or keyword in lowered for keyword in keywords)


def _columns_of_type(profile: DatasetProfile, semantic_types: tuple[str, ...]) -> list[str]:
    """画像里语义类型属于这几类的列名，**按画像里的原始顺序**。

    顺序很重要：它就是"默认列"的定义（第一个数值列、第一个分类列……）。
    这里不排序，否则默认列会随列名字典序变化，同一份数据两次跑出不同计划。
    """
    return [column.name for column in profile.columns if column.semantic_type in semantic_types]


def _pick_column(
    question: str, profile: DatasetProfile, semantic_types: tuple[str, ...]
) -> str | None:
    """选一列：**问题里提到的优先**，没提就取该语义类型的第一列；一类都没有则 `None`。"""
    candidates = _columns_of_type(profile, semantic_types)
    mentioned = _mentioned_column(question, candidates)
    if mentioned is not None:
        return mentioned
    return candidates[0] if candidates else None


def _mentioned_column(question: str, candidates: list[str]) -> str | None:
    """在问题里找候选列名，取**最长**的那个命中；没有命中返回 `None`。

    ## 为什么取"最长"而不是"第一个命中"

    两个反例，第二个才是真正会出错的：

    1. `销售额` 与 `销量` 有公共前缀，问题里同时出现"销量"和"销售额"时，
       按画像顺序取第一个命中，可能选中短的那个（用户问销售额，你给他算销量）。
    2. **一个列名是另一个列名的子串**：将来数据里加了 `利润率` 这一列，
       问题"各地区的利润率是多少"里，`利润` 也是它的子串。如果 `利润` 在画像里
       排在 `利润率` 前面，"第一个命中"就会选中 `利润` —— 算出来的数字很正常、
       没有报错、也没有告警，但答的不是用户问的问题。这种错只有逐字核对才发现得了。

    ## 只在 `candidates` 里找，不去问题里"猜"列名

    候选全部来自画像，所以匹配上的每个名字都真实存在于数据集里。反过来说，
    问题里出现的词如果不是真实列名（"按城市分析"而数据里只有 `地区`），这里就返回 `None`，
    规则会退回默认列或跳过 —— 绝不硬塞一个 `城市` 进去。

    同样长度的命中按"在问题里出现的位置"取靠前的那个，保证同一问题永远得到同一计划。
    """
    lowered = question.lower()
    hits = [name for name in candidates if name and name.lower() in lowered]
    if not hits:
        return None
    return max(hits, key=lambda name: (len(name), -lowered.index(name.lower())))
