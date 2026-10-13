"""描述性统计类工具：`describe_dataset`（五数概括）与 `missing_values`（缺失与重复）。

这两个工具只"看"数据：不建列、不改值、不写文件，也不出图（`needs_chart=False`）——
一行数字画成柱子只会让人误以为有结论。

参数进来之前已经被 `app/tools/validate.py` 校验过（工具名在不在白名单、类型与范围、
列存不存在、标了 `numeric_column` 的地方是不是数值列），所以这里**只做计算，不再做类型检查**：
同一套规则写两遍，早晚会不一致，而两边各自的测试都是绿的。

两个函数都不抛异常：算不出来就用 `None` 表示（`to_jsonable` 会把它变成 JSON 的 `null`）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools.jsonable import to_jsonable


def describe_dataset(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """对数值列做五数概括。

    `columns` 不传时默认用**全部数值列**（对文本列求均值没有意义，校验层也会拒绝显式传入）。
    标准差用样本标准差（`ddof=1`，与 `df.describe()` 一个口径）；有效值只有 1 个（或 0 个）时
    `std` 是 `None` —— 单个数字没有"波动"可言，硬给 0 会让人以为数据很稳。
    """
    columns = params.get("columns")
    if columns is None:
        columns = [str(name) for name in df.columns if pd.api.types.is_numeric_dtype(df[name])]

    stats: list[dict[str, Any]] = []
    for name in columns:
        values = df[name].dropna()
        count = int(values.count())
        stats.append(
            {
                "column": str(name),
                "count": count,
                "mean": _to_number(values.mean()),
                "std": _to_number(values.std(ddof=1)) if count > 1 else None,
                "min": _to_number(values.min()),
                "q1": _to_number(values.quantile(0.25)),
                "median": _to_number(values.median()),
                "q3": _to_number(values.quantile(0.75)),
                "max": _to_number(values.max()),
            }
        )
    return to_jsonable(
        {
            "row_count": int(len(df)),
            "columns": [item["column"] for item in stats],
            "stats": stats,
        }
    )


def missing_values(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """统计每列的缺失个数与比例，以及完全重复的行数。

    `columns` 不传时看**全部列**：文本列的缺失同样值得看（模型经常把空字符串当成有效的 0）。
    比例保留 4 位小数，与数据画像（`app/data/profiler.py`）的口径一致，两处数字可以直接对着看。
    """
    columns = params.get("columns")
    if columns is None:
        columns = [str(name) for name in df.columns]

    row_count = int(len(df))
    details: list[dict[str, Any]] = []
    for name in columns:
        null_count = int(df[name].isna().sum())
        details.append(
            {
                "column": str(name),
                "null_count": null_count,
                # 0 行的表算比例会除零，直接记 0.0（不是"没有缺失"，而是"没有数据"，
                # row_count=0 这个字段本身已经把话说清楚了）
                "null_ratio": round(null_count / row_count, 4) if row_count else 0.0,
            }
        )
    return to_jsonable(
        {
            "row_count": row_count,
            "duplicate_row_count": int(df.duplicated().sum()),
            "columns": details,
        }
    )


def check_describe_dataset(params: dict[str, Any], df: pd.DataFrame) -> str | None:
    """`describe_dataset` 的操作校验（校验 4）：返回一句中文错误消息 = 拒绝，返回 None = 通过。

    只有一种拒绝理由：不传 `columns` 时要"全部数值列"，而表里一个数值列都没有。
    显式传进来的列是不是数值列，由校验层按 schema 里的 `numeric_column` 统一挡。
    """
    if params.get("columns") is None and not any(
        pd.api.types.is_numeric_dtype(df[name]) for name in df.columns
    ):
        return "数据里没有任何数值列，做不了描述性统计（均值、分位数都算不了）"
    return None


def _to_number(value: Any) -> float | None:
    """把 pandas 算出来的标量变成 `float`；空值统一成 `None`（JSON 里没有 NaN）。"""
    if value is None or pd.isna(value):
        return None
    return float(value)
