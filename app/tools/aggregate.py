"""聚合类工具：`group_aggregate`（分组聚合）与 `top_n`（排序取前 N 行）。

两个工具的结果都天然适合画图（`needs_chart=True`）：分组结果是柱状图，排序结果是条形图。

分组结果的列名是**推导出来**的，不额外造名字：

- `metric="销售额"` + `agg="sum"` → 列名 `销售额_sum`
- 不传 `metric`（只允许配 `agg="count"`，表示"数每组有多少行"）→ 列名 `count`

这样命名有两个好处：列名里能看出算的是什么（`销售额_mean` 和 `销售额_sum` 不会混），
而且不用再发明"值/结果"这类没有信息量的名字。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools.jsonable import to_jsonable


def group_aggregate(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """按 1–3 列分组，对指标列做一次聚合，可按结果排序并限制返回的组数。

    - 不传 `metric` 时必须 `agg="count"`：这时数的是**每组的行数**（`.size()`），
      而不是某个列的非空个数。
    - `sort_by` 不传时按聚合结果自己排序（方向看 `sort_order`，默认从大到小）：
      "哪个地区销售额最高"这种问题，第一行就是答案。
    - `row_count` 是**返回的行数**（可能被 `limit` 截过），`group_count` 是分组后的总组数。
      两个都留着，是为了让"我只看到前 3 组"这件事在数据里看得见。
    """
    group_by = list(params["group_by"])
    metric = params.get("metric")
    agg = params["agg"]
    metric_column = f"{metric}_{agg}" if metric else "count"

    if metric:
        grouped = df.groupby(by=group_by, as_index=False, dropna=False, observed=True)[metric].agg(agg)
        grouped = grouped.rename(columns={metric: metric_column})
    else:
        # 不指定指标列时聚合的是"行数"；pandas 3.0 里 as_index=False 的 .size() 直接给"分组列 + size 列"
        grouped = df.groupby(by=group_by, as_index=False, dropna=False, observed=True).size()
        grouped = grouped.rename(columns={"size": metric_column})

    group_count = int(len(grouped))
    sort_by = params.get("sort_by")
    # sort_by 传的是"用户视角的列名"（分组列或指标列），这里翻译成结果表里的列名
    sort_column = metric_column if sort_by in (None, metric) else sort_by
    grouped = grouped.sort_values(
        by=sort_column,
        ascending=params.get("sort_order", "desc") == "asc",
        kind="stable",  # 稳定排序：值相同的组保持分组时的顺序，两次调用结果完全一样
    )

    limit = params.get("limit")
    if limit is not None:
        grouped = grouped.head(int(limit))

    return to_jsonable(
        {
            "columns": [str(name) for name in grouped.columns],
            "rows": grouped.to_dict(orient="records"),
            "row_count": int(len(grouped)),
            "group_count": group_count,
        }
    )


def top_n(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """按数值列排序，取前 N 行；给了 `group_by` 就是"每组各取前 N 行"。

    - 排序用稳定排序（`kind="stable"`）：值相同的行保持原顺序，同一条请求跑两次结果一致。
    - `columns` 不传时只返回"分组列 + 排序列"：模型问"销售额最高的 5 个商品"，
      要的是"商品 + 销售额"，把 9 列全塞回去只会浪费上下文。
    """
    by = params["by"]
    count = int(params["n"])
    ascending = bool(params.get("ascending", False))
    group_by = list(params.get("group_by") or [])

    ordered = df.sort_values(by=by, ascending=ascending, kind="stable")
    if group_by:
        # head 取的是"每个组在当前顺序里的前 n 行"，所以要先整体排序再分组取头
        picked = ordered.groupby(by=group_by, dropna=False, observed=True, sort=False).head(count)
    else:
        picked = ordered.head(count)

    columns = params.get("columns")
    if columns is None:
        columns = group_by + ([by] if by not in group_by else [])

    return to_jsonable(
        {
            "columns": [str(name) for name in columns],
            "rows": picked[list(columns)].to_dict(orient="records"),
            "row_count": int(len(picked)),
        }
    )


def check_group_aggregate(params: dict[str, Any], df: pd.DataFrame) -> str | None:
    """`group_aggregate` 的操作校验（校验 4）：返回错误消息 = 拒绝，返回 None = 通过。"""
    group_by = list(params["group_by"])
    metric = params.get("metric")
    agg = params["agg"]

    if metric is None:
        if agg != "count":
            return (
                f"agg={agg} 必须指定 metric：只有 count 可以不传指标列"
                "（不传 metric 时数的是每组行数）"
            )
        return None

    if metric in group_by:
        return (
            f"metric（{metric}）不能同时出现在 group_by 里："
            f"分组列在每个组里是常量，对它做 {agg} 没有意义"
        )

    sort_by = params.get("sort_by")
    if sort_by is not None and sort_by not in set(group_by) | {metric}:
        return (
            f"sort_by 只能是分组列或指标列（{'、'.join(group_by)}、{metric}），"
            f"实际是 {sort_by}"
        )
    return None


def check_top_n(params: dict[str, Any], df: pd.DataFrame) -> str | None:
    """`top_n` 的操作校验（校验 4）。"""
    group_by = list(params.get("group_by") or [])
    if params["by"] in group_by:
        return f"by（{params['by']}）不能出现在 group_by 里：在同一组内部按组名排序没有意义"
    return None
