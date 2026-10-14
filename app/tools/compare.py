"""比较类工具：`compare_groups`（分组比较）与 `correlation`（数值列两两相关）。

这两个工具回答的是"谁高谁低、谁和谁一起动"：

- `compare_groups` 把指标按一个分类列拆开，除了给出每组的聚合值，还给出**与各组平均水平的差值** ——
  "华东 32042" 是事实，"比各组平均低 4160" 才是结论，模型不用自己再算一遍。
- `correlation` 给 Pearson 相关系数矩阵，并把上三角摊平成 `pairs` 列表，方便模型直接引用某一对。

注意：**相关不等于因果**。这个工具只给数字，不给"因为……所以……"的解释（那是 LLM 的活，
而且它必须基于这些数字说话）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools.jsonable import to_jsonable

# 相关系数保留几位小数：4 位足够看出"强/弱"，再多只是噪声
CORRELATION_DECIMALS = 4


def compare_groups(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """按一个分类列比较某个数值指标。

    `差值` = 该组的值 − **各组值的平均**（不是"和总体平均比"）：分类不完全均衡时，
    各组值的平均才是"典型一组"的水平。正数表示高于这个水平。
    """
    group_by = params["group_by"]
    metric = params["metric"]
    agg = params.get("agg", "mean")
    top_k = int(params.get("top_k", 10))
    value_column = f"{metric}_{agg}"

    grouped = df.groupby(by=[group_by], as_index=False, dropna=False, observed=True)[metric].agg(agg)
    grouped = grouped.rename(columns={metric: value_column})

    average = float(grouped[value_column].mean())
    grouped["差值"] = (grouped[value_column] - average).round(4)
    grouped = grouped.sort_values(by=value_column, ascending=False, kind="stable").head(top_k)

    return to_jsonable(
        {
            "metric": str(metric),
            "columns": [str(group_by), value_column, "差值"],
            "rows": grouped.to_dict(orient="records"),
            "row_count": int(len(grouped)),
        }
    )


def correlation(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """算数值列两两之间的 Pearson 相关系数。

    `columns` 不传时用**全部数值列**。返回 `matrix`（行列顺序与 `columns` 一致，方便画热力图）
    和 `pairs`（只列上三角的每一对，方便模型直接说"销售额和利润的相关系数是 0.9872"）。
    只算 Pearson：它是这个项目里唯一一种相关系数，多给几种（spearman/kendall）只会让模型挑花眼。

    `pair_count` 与 `valid_pair_count` 是**显式给出的计数**，不是让调用方去数 `pairs`：
    报告里"共比较了 10 对"这句话里的 10，此前只能靠 `len(pairs)` 这个隐式结构计数
    得到，而校验层的隐式计数走的是"某个键有值的条数"——`r` 恰好为 float `0.0` 时
    （真的可能发生：两列完全无关）那个键是**假值**，计数就会比 `len(pairs)` 少，
    于是模板自己写的那句诚实的话会被校验层判成"编造的数字"。
    把计数变成工具输出里的**真实字段**，这个巧合就不存在了。
    `valid_pair_count` 是 `pairs` 里 `r is not None` 的条数（某列全空时 `r` 是 None），
    也就是 `_summarize_correlation` 说的"共比较了 N 对"里的那个 N。
    """
    columns = params.get("columns")
    if columns is None:
        columns = [str(name) for name in df.columns if pd.api.types.is_numeric_dtype(df[name])]
    columns = [str(name) for name in columns]

    # 校验层已经保证每一列都是数值列，所以这里不需要 numeric_only
    matrix_frame = df[columns].corr(method="pearson")

    matrix: list[list[float | None]] = []
    for row_name in columns:
        row: list[float | None] = []
        for column_name in columns:
            row.append(_round_correlation(matrix_frame.loc[row_name, column_name]))
        matrix.append(row)

    pairs: list[dict[str, Any]] = []
    for index, first in enumerate(columns):
        for second in columns[index + 1 :]:
            pairs.append(
                {
                    "x": first,
                    "y": second,
                    "r": _round_correlation(matrix_frame.loc[first, second]),
                }
            )

    # 上三角的每一对都进 `pairs`（包括 r 为 None 的），所以 `len(pairs)` 就是"一共
    # 比较了多少对"；`valid_pair_count` 是其中真的算出相关系数的那些。
    # 两个数都显式给出来，调用方不用去数列表长度 —— 数出来的长度在跨层传递时
    # 会因为"假值算不算有值"的口径差异而变（见 docstring）。
    pair_count = len(pairs)
    valid_pair_count = sum(1 for pair in pairs if pair["r"] is not None)

    return to_jsonable(
        {
            "columns": columns,
            "matrix": matrix,
            "pairs": pairs,
            "pair_count": pair_count,
            "valid_pair_count": valid_pair_count,
        }
    )


def check_correlation(params: dict[str, Any], df: pd.DataFrame) -> str | None:
    """`correlation` 的操作校验（校验 4）。"""
    if params.get("columns") is not None:
        # 显式传列时，列是不是数值列由校验层按 numeric_column 挡；数量的下限由 schema 的 min_items 挡
        return None
    numeric_count = sum(1 for name in df.columns if pd.api.types.is_numeric_dtype(df[name]))
    if numeric_count < 2:
        return f"数据里只有 {numeric_count} 个数值列，算不了相关系数（至少需要 2 个）"
    return None


def _round_correlation(value: Any) -> float | None:
    """相关系数保留 4 位小数；算不出来（某列全空）时是 `None`，不是 `nan`。"""
    if value is None or pd.isna(value):
        return None
    return round(float(value), CORRELATION_DECIMALS)
