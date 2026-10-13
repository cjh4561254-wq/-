"""工具注册表：整份"模型能用哪些工具"的白名单。

为什么必须有它（不是过度设计）
------------------------------
大模型能调用什么，只能来自这里：planner 拼 `tools=` 参数用 `to_openai_tools()`，
执行前用 `get(name)` 再查一次。**没有 `getattr(module, name)`、没有 eval** ——
"模型报了个工具名，我们就去某个模块里按名字取函数"是这一层最危险的做法：
模型完全可能报 `os.system`，而按名字取函数恰好能取到东西。注册表把它压成一次字典查询，
取不到就是 `TOOL_NOT_FOUND`，没有第二种可能。

两个附带好处：越权调用失败在**计划阶段**（`get` 返回 None），而不是执行到一半才发现；
工具的说明与参数 schema 和函数登记在同一处，改一个不用去同步第二个地方。

`params_schema` 是**自写的迷你格式**（不是标准 JSON Schema），只有 6 种类型：
`"str"` / `"int"` / `"float"` / `"bool"` / `"list[str]"` / `"enum"`。
它服务两件事：

1. `app/tools/validate.py` 按它做校验 2（类型/范围/枚举）与校验 3/4 的列标记；
2. `to_openai_tools()` 把它**翻译**成 OpenAI 要的标准 JSON Schema（见 `_to_json_schema`）——
   迷你格式里的 `min_items`、`column`、`"list[str]"` 标准 schema 都没有，
   直接丢给 OpenAI 会被当成无效 schema。

`func` 的参数顺序是 `(df, params)`，`check` 是 `(params, df)`：执行工具要先有数据，
而"操作合不合法"先看参数更自然。**这个差异是有意的，别顺手改成一样。**
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

import pandas as pd

from app.tools.aggregate import check_group_aggregate, check_top_n, group_aggregate, top_n
from app.tools.compare import check_correlation, compare_groups, correlation
from app.tools.describe import check_describe_dataset, describe_dataset, missing_values
from app.tools.timeseries import check_time_series_summary, time_series_summary

ToolCategory = Literal["profile", "aggregate", "rank", "quality", "compare", "trend"]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的"身份证"：模型看得到的说明 + 我们执行时用的函数。"""

    name: str
    description: str
    params_schema: dict[str, Any]
    func: Callable[[pd.DataFrame, dict[str, Any]], dict[str, Any]]
    category: ToolCategory
    needs_chart: bool = False
    check: Callable[[dict[str, Any], pd.DataFrame], str | None] | None = None


# 注册表本体：只有 register() 能往里写，别的地方一律读
_REGISTRY: dict[str, ToolSpec] = {}

# 迷你格式的类型名 → 标准 JSON Schema 的类型名
_TYPE_MAP = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def register(spec: ToolSpec) -> None:
    """登记一个工具。重名直接报错：两个同名工具意味着模型的选择永远有一半是错的。"""
    if spec.name in _REGISTRY:
        raise ValueError(f"工具名重复：{spec.name}")
    _REGISTRY[spec.name] = spec


def get(name: str) -> ToolSpec | None:
    """按名字取工具；取不到返回 None（**不抛异常**：调用方要用它做白名单判断）。"""
    _ensure_registered()
    return _REGISTRY.get(name)


def list_names() -> list[str]:
    """全部工具名，**已排序**（测试和展示都需要一个稳定顺序）。"""
    _ensure_registered()
    return sorted(_REGISTRY)


def list_specs() -> list[ToolSpec]:
    """全部工具，按名字排序（顺序稳定，方便逐个遍历）。"""
    _ensure_registered()
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def to_openai_tools() -> list[dict[str, Any]]:
    """转成 OpenAI function-calling 的 `tools` 参数（D4 调模型时用）。"""
    _ensure_registered()
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": _to_json_schema(spec.params_schema),
            },
        }
        for spec in list_specs()
    ]


def register_all() -> None:
    """登记全部 7 个内置工具。**幂等**：重复调用不报错，也不会登记第二遍。"""
    for spec in _builtin_specs():
        if spec.name not in _REGISTRY:
            register(spec)


def _ensure_registered() -> None:
    """第一次读注册表时自动装载内置工具（实现细节，不在公开接口里）。

    为什么要兜这一下：忘了调用 `register_all()` 的表现是"7 个工具全部报 TOOL_NOT_FOUND"，
    排查方向会被带偏到"是不是模型把工具名说错了"。register_all 本身是幂等的，兜一下没有代价。
    """
    if not _REGISTRY:
        register_all()


def _to_json_schema(params_schema: dict[str, Any]) -> dict[str, Any]:
    """把自写的迷你 schema 翻译成标准 JSON Schema（OpenAI 只认后者）。

    `column` / `numeric_column` 这类内部标记不翻译出去：它们是"给我们自己校验用"的，
    对模型没有意义（真正有用的提示写在 `description` 里）。
    """
    properties: dict[str, Any] = {}
    for name, rule in params_schema.get("properties", {}).items():
        kind = rule["type"]
        if kind == "enum":
            json_rule: dict[str, Any] = {"type": "string", "enum": list(rule["values"])}
        elif kind == "list[str]":
            json_rule = {"type": "array", "items": {"type": "string"}}
            if "min_items" in rule:
                json_rule["minItems"] = rule["min_items"]
            if "max_items" in rule:
                json_rule["maxItems"] = rule["max_items"]
        else:
            json_rule = {"type": _TYPE_MAP[kind]}
            if "min" in rule:
                json_rule["minimum"] = rule["min"]
            if "max" in rule:
                json_rule["maximum"] = rule["max"]
        if "description" in rule:
            json_rule["description"] = rule["description"]
        if "default" in rule:
            json_rule["default"] = rule["default"]
        properties[name] = json_rule

    return {
        "type": "object",
        "properties": properties,
        "required": list(params_schema.get("required", [])),
    }


def _builtin_specs() -> list[ToolSpec]:
    """7 个内置工具的定义。

    这里是**硬编码**的：不扫目录、不按文件名自动导入。多一个工具就要在这里多一行，
    而"多一行"正是我们想要的 —— 白名单变长必须是有人主动改代码，不能是碰巧多个文件。
    """
    return [
        ToolSpec(
            name="describe_dataset",
            description=(
                "对数值列做描述性统计：有效值个数、均值、标准差、最小值、四分位、中位数、最大值。"
                "想先知道“这个指标大概什么水平、波动多大”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "list[str]",
                        "min_items": 1,
                        "column": True,
                        "numeric_column": True,
                        "description": "要统计的数值列；不传则统计全部数值列",
                    }
                },
                "required": [],
            },
            func=describe_dataset,
            category="profile",
            needs_chart=False,
            check=check_describe_dataset,
        ),
        ToolSpec(
            name="group_aggregate",
            description=(
                "按 1–3 个列分组，对某个数值列做聚合（求和/均值/计数/最小/最大/中位数），"
                "可按结果排序并限制返回的组数。"
                "想知道“各地区销售额分别是多少”“哪类商品卖得最多”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "list[str]",
                        "min_items": 1,
                        "max_items": 3,
                        "column": True,
                        "description": "按哪些列分组",
                    },
                    "metric": {
                        "type": "str",
                        "column": True,
                        "numeric_column": True,
                        "description": "要聚合的数值列；不传（只配 agg=count）表示只数每组行数",
                    },
                    "agg": {
                        "type": "enum",
                        "values": ["sum", "mean", "count", "min", "max", "median"],
                        "description": "聚合方式",
                    },
                    "limit": {
                        "type": "int",
                        "min": 1,
                        "max": 100,
                        "description": "最多返回多少组",
                    },
                    "sort_by": {
                        "type": "str",
                        "column": True,
                        "description": "按哪一列排序（分组列或指标列）；不传则按聚合结果排序",
                    },
                    "sort_order": {
                        "type": "enum",
                        "values": ["asc", "desc"],
                        "default": "desc",
                        "description": "排序方向",
                    },
                },
                "required": ["group_by", "agg"],
            },
            func=group_aggregate,
            category="aggregate",
            needs_chart=True,
            check=check_group_aggregate,
        ),
        ToolSpec(
            name="top_n",
            description=(
                "按数值列排序取前 N 行；给了 group_by 就是“每组各取前 N 行”。"
                "想知道“销售额最高的 5 个商品”“每个地区卖得最好的商品”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "by": {
                        "type": "str",
                        "column": True,
                        "numeric_column": True,
                        "description": "按这一列排序（必须是数值列）",
                    },
                    "n": {"type": "int", "min": 1, "max": 50, "description": "每个组取多少行"},
                    "ascending": {
                        "type": "bool",
                        "default": False,
                        "description": "True 表示从小到大（默认从大到小）",
                    },
                    "group_by": {
                        "type": "list[str]",
                        "min_items": 1,
                        "max_items": 3,
                        "column": True,
                        "description": "先按这些列分组，每组各取 n 行",
                    },
                    "columns": {
                        "type": "list[str]",
                        "min_items": 1,
                        "column": True,
                        "description": "结果里要哪些列；不传则给分组列 + 排序列",
                    },
                },
                "required": ["by", "n"],
            },
            func=top_n,
            category="rank",
            needs_chart=True,
            check=check_top_n,
        ),
        ToolSpec(
            name="missing_values",
            description=(
                "统计每列的缺失个数与比例，以及完全重复的行数。"
                "分析前先看数据质量、或者发现“合计对不上”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "list[str]",
                        "min_items": 1,
                        "column": True,
                        "description": "要看哪些列；不传则看全部列",
                    }
                },
                "required": [],
            },
            func=missing_values,
            category="quality",
            needs_chart=False,
            check=None,
        ),
        ToolSpec(
            name="compare_groups",
            description=(
                "把一个数值指标按一个分类列拆开比较，除了给出每组的聚合值，还给出"
                "“与各组平均水平的差值”。想知道“哪个地区客单价最高”“哪个渠道转化最好”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "group_by": {
                        "type": "str",
                        "column": True,
                        "description": "按哪个分类列分组",
                    },
                    "metric": {
                        "type": "str",
                        "column": True,
                        "numeric_column": True,
                        "description": "要比较的数值列",
                    },
                    "agg": {
                        "type": "enum",
                        "values": ["mean", "sum", "count", "min", "max", "median"],
                        "default": "mean",
                        "description": "聚合方式",
                    },
                    "top_k": {
                        "type": "int",
                        "min": 1,
                        "max": 30,
                        "description": "最多返回多少组",
                    },
                },
                "required": ["group_by", "metric"],
            },
            func=compare_groups,
            category="compare",
            needs_chart=True,
            check=None,
        ),
        ToolSpec(
            name="correlation",
            description=(
                "算数值列两两之间的 Pearson 相关系数（-1 到 1，0 表示没有线性关系）。"
                "想知道“销售额和利润是不是一起涨”“哪个指标和流失率关系最强”时用它。"
                "注意：相关不等于因果，结论要靠数据说话。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "columns": {
                        "type": "list[str]",
                        "min_items": 2,
                        "column": True,
                        "numeric_column": True,
                        "description": "要算相关的数值列（至少 2 列）；不传则用全部数值列",
                    }
                },
                "required": [],
            },
            func=correlation,
            category="compare",
            needs_chart=True,
            check=check_correlation,
        ),
        ToolSpec(
            name="time_series_summary",
            description=(
                "按日/周/月汇总一个数值指标，可选算环比。"
                "想知道“销售额这几个月是涨还是跌”“哪一周最忙”时用它。"
            ),
            params_schema={
                "type": "object",
                "properties": {
                    "date_column": {
                        "type": "str",
                        "column": True,
                        "description": "日期列（可以带空值，解析不出来的行会被跳过）",
                    },
                    "metric": {
                        "type": "str",
                        "column": True,
                        "numeric_column": True,
                        "description": "要汇总的数值列",
                    },
                    "freq": {
                        "type": "enum",
                        "values": ["D", "W", "M"],
                        "default": "M",
                        "description": "汇总粒度：D=按日，W=按周，M=按月",
                    },
                    "agg": {
                        "type": "enum",
                        "values": ["sum", "mean", "count", "min", "max", "median"],
                        "default": "sum",
                        "description": "聚合方式",
                    },
                    "include_growth": {
                        "type": "bool",
                        "default": False,
                        "description": "True 则多返回一列环比（本期相对上一期的变化比例）",
                    },
                },
                "required": ["date_column", "metric"],
            },
            func=time_series_summary,
            category="trend",
            needs_chart=True,
            check=check_time_series_summary,
        ),
    ]
