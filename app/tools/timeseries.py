"""时间序列工具：`time_series_summary`（按日/周/月汇总一个指标）。

这个工具回答"趋势"类问题："销售额这几个月是涨还是跌"。

三个刻意定下来的口径：

1. **日期解析不出来的行会被跳过**，不算进任何一个时间点。这不是"丢数据"：`2024-13-45`
   这种值本来就落不到时间轴上。跳过的行数放在 `skipped_row_count` 里，让"月度合计比总额少"
   这件事在结果里看得见，而不是让人以为工具算错了。
2. **周期标签用字符串**：`D` → `2024-01-05`、`W` → `2024-W01`（ISO 周）、`M` → `2024-01`。
   字符串按字典序排就是按时间排（标签里都带年份/补零），不用再维护一个 Period 类型，
   也不会出现"pandas 的 Period 对象进不了 JSON"的问题。
3. **环比是比值不是百分比**：`0.1116` 表示比上一期涨了 11.16%。第一期没有"上一期"，记 `None`；
   上一期为 0 时除不了（会变成无穷大），也记 `None`。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.tools.jsonable import to_jsonable

# 环比保留几位小数：4 位够看到"涨了几个点"，再多位只是噪声
GROWTH_DECIMALS = 4
GROWTH_COLUMN = "环比"


def time_series_summary(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """按 `freq`（D 日 / W 周 / M 月）汇总 `metric`，可选算环比。"""
    date_column = params["date_column"]
    metric = params["metric"]
    freq = params.get("freq", "M")
    agg = params.get("agg", "sum")
    metric_column = f"{metric}_{agg}"

    dates = pd.to_datetime(df[date_column], errors="coerce")
    valid = dates.notna()

    # 只把"能落到时间轴上"的行拿来做汇总；行号保持原样，靠同一个 mask 对齐
    working = pd.DataFrame(
        {
            "period": _period_labels(dates[valid], freq),
            "value": df.loc[valid, metric],
        }
    )
    grouped = (
        working.groupby(by=["period"], as_index=False, dropna=False, observed=True)["value"]
        .agg(agg)
        .rename(columns={"value": metric_column})
    )
    # 标签本身就是按时间顺序排的字符串（年份 + 补零），所以直接按标签排
    grouped = grouped.sort_values(by="period", ascending=True, kind="stable")

    columns = ["period", metric_column]
    if params.get("include_growth", False):
        grouped[GROWTH_COLUMN] = _period_growth(grouped[metric_column])
        columns.append(GROWTH_COLUMN)

    return to_jsonable(
        {
            "columns": columns,
            "rows": grouped.to_dict(orient="records"),
            "row_count": int(len(grouped)),
            "skipped_row_count": int((~valid).sum()),
        }
    )


def check_time_series_summary(params: dict[str, Any], df: pd.DataFrame) -> str | None:
    """`time_series_summary` 的操作校验（校验 4）。"""
    date_column = params["date_column"]

    if pd.api.types.is_numeric_dtype(df[date_column]):
        # 真踩过：pd.to_datetime([1, 2, 3]) 会把整数当成"1970 年起的纳秒偏移"，
        # 于是编号列被解析成 1970-01-01 附近的日期，看起来还挺像那么回事
        return (
            f"列 {date_column} 是数值列，不是日期列："
            "pd.to_datetime 会把整数当成 1970 年起的纳秒偏移，得到的日期没有意义"
        )

    if not pd.to_datetime(df[date_column], errors="coerce").notna().any():
        return (
            f"列 {date_column} 里没有一个值能解析成日期，做不了时间序列；"
            "建议换一列，或者先把日期统一成 YYYY-MM-DD 再上传"
        )
    return None


def _period_labels(dates: pd.Series, freq: str) -> pd.Series:
    """把日期列转成周期标签字符串（D/W/M 三种）。"""
    if freq == "D":
        return dates.dt.strftime("%Y-%m-%d")
    if freq == "W":
        iso = dates.dt.isocalendar()
        # ISO 周：2024-W01。用 isocalendar 而不是 "第几周"，跨年那几周才不会串到上一年
        return iso["year"].astype("Int64").astype(str) + "-W" + iso["week"].astype("Int64").astype(str).str.zfill(2)
    return dates.dt.to_period("M").astype(str)


def _period_growth(values: pd.Series) -> list[float | None]:
    """逐期环比 =（本期 − 上期）/ 上期。

    第一期没有上期 → `None`；上期为 0（除不了）或本期为空 → `None`。
    返回列表而不是 Series：它要按位置塞回 `grouped` 的新列里，位置对齐最直观。
    """
    numbers: list[float | None] = [None if pd.isna(value) else float(value) for value in values]
    growth: list[float | None] = [None]
    for index in range(1, len(numbers)):
        previous = numbers[index - 1]
        current = numbers[index]
        if previous is None or current is None or previous == 0:
            growth.append(None)
        else:
            growth.append(round((current - previous) / previous, GROWTH_DECIMALS))
    return growth
