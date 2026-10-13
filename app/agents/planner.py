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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from app import config
from app.schemas_dataset import DatasetProfile
from app.schemas_workflow import AnalysisTask, TaskPlan

# TaskPlan.tasks 的硬上限（契约里是 min_length=1 / max_length=8）。
# `max_steps` 是请求侧的旋钮，但绝不能让它把计划顶破契约：传 0 会得到空计划、
# 传 20 会得到 9 个任务，两种都会让 TaskPlan 直接抛 ValidationError。
# 这里把它夹到 1..8，"绝不允许空计划"才是一句有代码保证的话。
_MIN_TASKS = 1
_MAX_TASKS = 8

# 一条都没命中时的兜底任务：至少给用户一份描述性统计，
# 而不是回一句"我不知道你想问什么"——那是产品失败，不是数据失败。
_FALLBACK_DESCRIPTION = "对每个数值列做描述性统计（计数、均值、分位数、最大最小值）"


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
