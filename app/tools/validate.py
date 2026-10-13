"""工具调用的校验：校验 1–4，按顺序前置短路。

为什么顺序不能变
----------------
**工具名 → 参数 → 列存在 → 操作合法**。前面的过了才轮到后面的：

- 名字都不认识，讨论参数没有意义；
- 参数类型都不对（比如 `n` 传了 `"5"`），先报列不存在只会让人查错方向；
- 列都不存在，就谈不上"对文本列求均值"这种操作问题。

**前置短路**还有个实际好处：报给模型的是"第一个真正的错误"，而不是一堆错误的拼接 ——
一次改一处，比同时改五处容易。

校验 5（返回结构）不在这里：它由 `app/tools/jsonable.py` 的 `ensure_jsonable` 在
D4 的 `analysis.py` 收尾时执行（架构文档 §6.3），本模块只负责前四类。

错误码（与 `app/schemas_tool.py` 的 `ToolErrorCode` 一致）
--------------------------------------------------------
- `TOOL_NOT_FOUND`：校验 1 —— 白名单里没有这个工具
- `BAD_PARAMS`：校验 2 —— 缺参数、多参数、类型/范围/枚举不对
- `COLUMN_NOT_FOUND`：校验 3 —— 列名不存在（消息里附真实列名，最多 20 个）
- `BAD_OPERATION`：校验 4 —— 操作本身不合法（对文本列求均值、metric 和 group_by 是同一列……）

两条刻意的严格
--------------
1. **类型严格**：`n` 要整数，传 `10.0`（JSON 里的浮点数）会被拒。宁可让模型把参数写对，
   也不要悄悄替它改 —— 悄悄改会让"模型到底传了什么"变得不可复盘。
2. **不填默认值**：schema 里的 `default` 是写给模型和工具函数看的，工具函数自己
   `params.get("agg", "mean")`。校验层要是替它填上，"模型没传"和"模型传了默认值"
   就长得一模一样，排查时分不清这是谁的决定。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools import registry
from app.tools.registry import ToolSpec


class ToolValidationError(Exception):
    """校验 1–4 不通过。

    名字里带 Validation 是为了和 `app/errors.py` 的 `AppError` 区分开：这个异常**不直接**
    对应 HTTP 状态码，它对应"某个分析任务失败了" —— 由 D4 捕获后写进 `ToolResult.error`，
    整条工作流继续跑下一个任务（一个任务失败不该让整个分析失败）。
    """

    def __init__(self, error_code: str, error_message: str, detail: str | None = None) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message
        self.detail = detail


def validate_call(tool_name: str, params: dict[str, Any], df: pd.DataFrame) -> ToolSpec:
    """校验 1–4；通过返回对应的 `ToolSpec`，失败抛 `ToolValidationError`。"""
    spec = registry.get(tool_name)  # 校验 1：白名单（没有 getattr / eval，见 registry 的说明）
    if spec is None:
        raise ToolValidationError(
            "TOOL_NOT_FOUND",
            f"没有这个工具：{tool_name}。可用工具：{'、'.join(registry.list_names())}",
            detail="工具名必须来自注册表，不能是任意字符串",
        )
    _validate_params(spec, params)  # 校验 2
    _validate_columns(spec, params, df)  # 校验 3
    _validate_operation(spec, params, df)  # 校验 4
    return spec


# ---------------------------------------------------------------- 校验 2：参数


def _validate_params(spec: ToolSpec, params: dict[str, Any]) -> None:
    """按 schema 检查参数：必须带齐 required、不能多、类型/范围/枚举要对。"""
    if not isinstance(params, dict):
        raise ToolValidationError(
            "BAD_PARAMS",
            f"参数必须是一个对象，实际是 {type(params).__name__}",
        )

    schema = spec.params_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required = list(schema.get("required", []))

    for name in required:
        if name not in params:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"缺少必填参数 {name}；{spec.name} 需要 {'、'.join(required)}",
            )

    for name, value in params.items():
        if name not in properties:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"不认识的参数 {name}；{spec.name} 只接受 {'、'.join(properties)}",
            )
        _check_one_param(name, value, properties[name])


def _check_one_param(name: str, value: Any, rule: dict[str, Any]) -> None:
    """检查一个参数的取值。`rule` 就是 schema 里那一条（迷你格式）。"""
    kind = rule["type"]

    if kind == "enum":
        if value not in rule["values"]:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"参数 {name} 只能是 {'/'.join(rule['values'])} 之一，实际是 {value!r}",
            )
        return

    if kind == "list[str]":
        if not isinstance(value, list):
            raise _type_error(name, "list[str]", value)
        for item in value:
            if not isinstance(item, str):
                raise _type_error(name, "list[str]", item, "列表里出现了非字符串")
        size = len(value)
        if size < rule.get("min_items", 0):
            raise ToolValidationError(
                "BAD_PARAMS",
                f"参数 {name} 至少要 {rule['min_items']} 项，实际只有 {size} 项",
            )
        if "max_items" in rule and size > rule["max_items"]:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"参数 {name} 最多 {rule['max_items']} 项，实际给了 {size} 项",
            )
        return

    if kind == "bool":
        if not isinstance(value, bool):
            raise _type_error(name, "bool", value)
        return
    if kind == "int":
        # bool 必须单独挡：Python 里 True 是 int 的子类（isinstance(True, int) 是 True），
        # 不挡的话 `n=True` 会被当成 `n=1` 收下
        if isinstance(value, bool) or not isinstance(value, int):
            raise _type_error(name, "int", value)
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _type_error(name, "float", value)
    elif kind == "str":
        if not isinstance(value, str):
            raise _type_error(name, "str", value)
    else:
        # schema 里写了一个不认识类型名 = 开发期错误，不是模型的问题
        raise ToolValidationError("BAD_PARAMS", f"schema 里的类型名不认识：{kind!r}")

    if kind in ("int", "float"):
        if "min" in rule and value < rule["min"]:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"参数 {name} 不能小于 {rule['min']}，实际是 {value!r}",
            )
        if "max" in rule and value > rule["max"]:
            raise ToolValidationError(
                "BAD_PARAMS",
                f"参数 {name} 不能大于 {rule['max']}，实际是 {value!r}",
            )


def _type_error(name: str, expected: str, value: Any, reason: str = "") -> ToolValidationError:
    """统一"类型不对"的消息格式：期望什么、实际是什么、值长什么样。"""
    text = f"参数 {name} 类型不对：期望 {expected}，实际是 {type(value).__name__}（值 {value!r}）"
    if reason:
        text = f"{text}；{reason}"
    return ToolValidationError("BAD_PARAMS", text)


# ---------------------------------------------------------------- 校验 3：列


def _validate_columns(spec: ToolSpec, params: dict[str, Any], df: pd.DataFrame) -> None:
    """schema 里标了 `column: True` 的参数，值必须是 `df.columns` 里的真实列名。"""
    known = {str(name) for name in df.columns}
    for name, rule in _column_rules(spec):
        value = params.get(name)
        if value is None:
            continue  # 可选参数没传就跳过
        missing = [item for item in _as_column_list(rule, value) if item not in known]
        if missing:
            raise ToolValidationError(
                "COLUMN_NOT_FOUND",
                f"数据集里没有这些列：{'、'.join(missing)}。真实列名是：{_column_list_text(df)}",
                detail=f"数据集一共 {len(known)} 列",
            )


def _column_rules(spec: ToolSpec) -> list[tuple[str, dict[str, Any]]]:
    """取出所有"值是列名"的参数（schema 里标了 `column` 的那些）。"""
    return [
        (name, rule)
        for name, rule in spec.params_schema.get("properties", {}).items()
        if rule.get("column")
    ]


def _as_column_list(rule: dict[str, Any], value: Any) -> list[str]:
    """把"一个列名"和"一串列名"统一成列表：`list[str]` 参数是一串，其余是一个。"""
    if rule["type"] == "list[str]":
        return list(value)
    return [value]


def _column_list_text(df: pd.DataFrame, limit: int = 20) -> str:
    """把真实列名拼给模型看，最多 20 个（列多的时候全列出来会淹没错误信息本身）。"""
    names = [str(name) for name in df.columns]
    text = "、".join(names[:limit])
    if len(names) > limit:
        text += f"（还有 {len(names) - limit} 列没列出来，一共 {len(names)} 列）"
    return text


# ---------------------------------------------------------------- 校验 4：操作


def _validate_operation(spec: ToolSpec, params: dict[str, Any], df: pd.DataFrame) -> None:
    """先跑两条与工具无关的通用规则，再把话语权交给工具自己的 `check`。

    通用规则：
    - 列表参数里不能重复（`group_by=["地区","地区"]` 只会得到一张有重复列的表）；
    - 标了 `numeric_column` 的参数必须是数值列（对文本列求均值是经典错误）。

    工具特有的规则写在各自的 `check` 里（返回一句中文错误消息 = 拒绝），
    这样 validate.py 不需要认识"哪个工具不能拿 group_by 当 metric"这类业务细节。
    """
    for name, rule in _column_rules(spec):
        value = params.get(name)
        if value is None:
            continue
        values = _as_column_list(rule, value)
        if len(set(values)) != len(values):
            raise ToolValidationError(
                "BAD_OPERATION",
                f"参数 {name} 里有重复的列：{'、'.join(values)}",
            )
        if rule.get("numeric_column"):
            not_numeric = [
                item for item in values if not pd.api.types.is_numeric_dtype(df[item])
            ]
            if not_numeric:
                raise ToolValidationError(
                    "BAD_OPERATION",
                    f"列 {'、'.join(not_numeric)} 不是数值列，做不了这种计算；"
                    f"数值列是：{_numeric_columns_text(df)}",
                    detail=f"参数 {name} 需要数值列",
                )

    if spec.check is not None:
        message = spec.check(params, df)
        if message:
            raise ToolValidationError("BAD_OPERATION", message)


def _numeric_columns_text(df: pd.DataFrame) -> str:
    """数据里全部数值列的名字（一个都没有时说清楚，而不是给个空字符串）。"""
    names = [str(name) for name in df.columns if pd.api.types.is_numeric_dtype(df[name])]
    return "、".join(names) if names else "（一个都没有）"
