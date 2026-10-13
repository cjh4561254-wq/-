"""`app/tools/` 的 7 个工具：数字对账 + 4 类校验 + 返回值可序列化。

为什么断言里要写具体数字
------------------------
只断言"返回了 6 行"是**假绿**：聚合算错列、把 sum 写成 mean，形状常常照样对。
所以这里每个工具都在 `data/samples/sales.csv`（2168 行真实数据）上对一遍
**独立算出来的结果**：要么在测试里用 pandas 另算一遍，要么对着
`docs/STAGE2_ACCEPTANCE.md` 里第二阶段门禁实测的那几个数字。

测试里出现的每个固定数字，来源都是这次真实跑出来的结果，不是手抄想象值。
"""

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app import config
from app.tools.jsonable import ensure_jsonable, to_jsonable
from app.tools.registry import to_openai_tools
from app.tools.validate import ToolValidationError, validate_call

SAMPLE_CSV = config.PROJECT_ROOT / "data" / "samples" / "sales.csv"

# docs/STAGE2_ACCEPTANCE.md 里记的门禁数字：describe_dataset(columns=["销售额"])
SALES_MEAN = 36155.75720930233
SALES_MEDIAN = 22267.0
SALES_NULL_COUNT = 18
DUPLICATE_ROWS = 8

REGION_SUMS = {
    "华南": 24871657.0,
    "东北": 15890839.0,
    "华东": 11567345.0,
    "华北": 8948124.0,
    "西南": 8467097.0,
    "西北": 7989816.0,
}
REGION_COUNTS = {"西南": 363, "华北": 362, "东北": 361, "华东": 361, "西北": 361, "华南": 360}
REGION_MEANS = {
    "华南": 70061.00563380282,
    "东北": 44141.21944444445,
    "华东": 32042.506925207756,
    "华北": 25064.7731092437,
    "西南": 23651.108938547484,
    "西北": 22255.75487465181,
}
MONTHLY_SUMS = {
    "2024-01": 3398283.0,
    "2024-02": 3777688.0,
    "2024-03": 4780531.0,
    "2024-04": 13319512.0,
    "2024-05": 5135498.0,
    "2024-06": 5319825.0,
    "2024-07": 6101065.0,
    "2024-08": 4999075.0,
    "2024-09": 19186480.0,
    "2024-10": 3826473.0,
    "2024-11": 4013012.0,
    "2024-12": 3196608.0,
}

# 每个工具一份"能跑通"的参数（用来统一验证可序列化）
VALID_CALLS = {
    "describe_dataset": {},
    "group_aggregate": {"group_by": ["地区"], "metric": "销售额", "agg": "sum"},
    "top_n": {"by": "销售额", "n": 3},
    "missing_values": {},
    "compare_groups": {"group_by": "地区", "metric": "销售额"},
    "correlation": {},
    "time_series_summary": {"date_column": "日期", "metric": "销售额"},
}

# 每个工具一份"参数不合法"的调用：都必须 BAD_PARAMS
BAD_PARAMS_CALLS = {
    "describe_dataset": {"columns": "销售额"},  # 该传 list 却传了 str
    "group_aggregate": {"group_by": ["地区"]},  # 缺必填 agg
    "top_n": {"by": "销售额", "n": 0},  # n 小于下限
    "missing_values": {"columns": "地区"},
    "compare_groups": {"group_by": "地区"},  # 缺必填 metric
    "correlation": {"columns": ["销售额"]},  # 至少 2 列
    "time_series_summary": {"date_column": "日期", "metric": "销售额", "freq": "Q"},  # 枚举外
}

# 每个工具一份"列不存在"的调用：都必须 COLUMN_NOT_FOUND
MISSING_COLUMN_CALLS = {
    "describe_dataset": {"columns": ["不存在的列"]},
    "group_aggregate": {"group_by": ["不存在的列"], "agg": "count"},
    "top_n": {"by": "不存在的列", "n": 3},
    "missing_values": {"columns": ["不存在的列"]},
    "compare_groups": {"group_by": "不存在的列", "metric": "销售额"},
    "correlation": {"columns": ["销售额", "不存在的列"]},
    "time_series_summary": {"date_column": "不存在的列", "metric": "销售额"},
}


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return pd.read_csv(SAMPLE_CSV, encoding="utf-8-sig")


def call(df: pd.DataFrame, name: str, params: dict[str, Any]) -> dict[str, Any]:
    """走一遍真实执行路径：校验 1–4 → 算 → `ensure_jsonable` 复核。"""
    spec = validate_call(name, params, df)
    data = spec.func(df, params)
    ensure_jsonable(data)
    return data


def expect_code(df: pd.DataFrame, name: str, params: dict[str, Any], code: str) -> str:
    """断言校验失败且错误码正确，返回错误消息（方便继续断言消息内容）。"""
    with pytest.raises(ToolValidationError) as excinfo:
        validate_call(name, params, df)
    assert excinfo.value.error_code == code
    return excinfo.value.error_message


# ============================================================ describe_dataset


def test_describe_sales_matches_the_acceptance_numbers(df: pd.DataFrame) -> None:
    """要求 3：销售额的 mean / median 必须等于门禁实测的那两个数。"""
    data = call(df, "describe_dataset", {"columns": ["销售额"]})
    assert data["row_count"] == 2168
    stats = data["stats"][0]
    assert stats["mean"] == pytest.approx(SALES_MEAN)
    assert stats["median"] == pytest.approx(SALES_MEDIAN)


def test_describe_five_numbers_match_pandas(df: pd.DataFrame) -> None:
    """独立用 pandas 再算一遍，逐项对数（不只看形状）。"""
    stats = call(df, "describe_dataset", {"columns": ["销售额"]})["stats"][0]
    column = df["销售额"]
    assert stats["count"] == int(column.count())
    assert stats["std"] == pytest.approx(float(column.std(ddof=1)))
    assert stats["min"] == pytest.approx(float(column.min()))
    assert stats["q1"] == pytest.approx(float(column.quantile(0.25)))
    assert stats["q3"] == pytest.approx(float(column.quantile(0.75)))
    assert stats["max"] == pytest.approx(float(column.max()))


def test_describe_defaults_to_all_numeric_columns(df: pd.DataFrame) -> None:
    data = call(df, "describe_dataset", {})
    assert data["columns"] == ["销量", "单价", "销售额", "利润", "客户评分"]
    assert [item["column"] for item in data["stats"]] == data["columns"]


def test_describe_std_is_none_when_only_one_value_is_left() -> None:
    """只有一个有效值时 ddof=1 的标准差是 NaN，清洗后必须是 None（不能漏成 NaN）。"""
    frame = pd.DataFrame({"只有一格": [5.0, np.nan, np.nan]})
    stats = call(frame, "describe_dataset", {"columns": ["只有一格"]})["stats"][0]
    assert stats["count"] == 1
    assert stats["std"] is None
    assert stats["mean"] == pytest.approx(5.0)


def test_describe_rejects_a_text_column(df: pd.DataFrame) -> None:
    message = expect_code(df, "describe_dataset", {"columns": ["地区"]}, "BAD_OPERATION")
    assert "不是数值列" in message


# ============================================================= missing_values


def test_missing_values_sales_null_count_and_duplicates(df: pd.DataFrame) -> None:
    """要求 7：销售额缺失必须是 18，完全重复行必须是 8。"""
    data = call(df, "missing_values", {})
    by_column = {item["column"]: item for item in data["columns"]}
    assert by_column["销售额"]["null_count"] == SALES_NULL_COUNT
    assert data["duplicate_row_count"] == DUPLICATE_ROWS
    assert data["row_count"] == 2168


def test_missing_values_ratios_are_hand_computed(df: pd.DataFrame) -> None:
    """比例是"缺失数 ÷ 总行数"保留 4 位小数，逐列对一遍。"""
    data = call(df, "missing_values", {})
    for item in data["columns"]:
        expected = round(int(df[item["column"]].isna().sum()) / len(df), 4)
        assert item["null_ratio"] == pytest.approx(expected), item["column"]
        assert item["null_count"] == int(df[item["column"]].isna().sum()), item["column"]
    assert {item["column"]: item["null_count"] for item in data["columns"] if item["null_count"]} == {
        "日期": 24,
        "销量": 18,
        "销售额": 18,
        "利润": 18,
        "客户评分": 27,
    }


def test_missing_values_can_look_at_selected_columns(df: pd.DataFrame) -> None:
    data = call(df, "missing_values", {"columns": ["客户评分"]})
    assert [item["column"] for item in data["columns"]] == ["客户评分"]
    assert data["columns"][0]["null_count"] == 27


# =========================================================== group_aggregate


def test_group_aggregate_sums_match_hand_computation(df: pd.DataFrame) -> None:
    """要求 2：每一行数字都要与独立算出来的一致。"""
    data = call(df, "group_aggregate", {"group_by": ["地区"], "metric": "销售额", "agg": "sum"})
    assert data["columns"] == ["地区", "销售额_sum"]
    assert data["group_count"] == 6
    assert data["row_count"] == 6
    got = {row["地区"]: row["销售额_sum"] for row in data["rows"]}
    expected = df.groupby(by=["地区"], as_index=False, dropna=False, observed=True)["销售额"].agg("sum")
    for row in expected.itertuples(index=False):
        assert got[row[0]] == pytest.approx(float(row[1]))
    assert got == pytest.approx(REGION_SUMS)


def test_group_aggregate_sums_equal_the_quantity_times_price_path(df: pd.DataFrame) -> None:
    """要求 5：数据集保证 销售额 = 销量 × 单价，两条路径的分组求和必须完全相等。"""
    data = call(df, "group_aggregate", {"group_by": ["地区"], "metric": "销售额", "agg": "sum"})
    got = {row["地区"]: row["销售额_sum"] for row in data["rows"]}
    by_path = (df["销量"] * df["单价"]).groupby(df["地区"], dropna=False).sum()
    for region, value in by_path.items():
        assert got[region] == pytest.approx(float(value), rel=1e-12), region


def test_group_aggregate_count_needs_no_metric_and_covers_every_row(df: pd.DataFrame) -> None:
    data = call(df, "group_aggregate", {"group_by": ["地区"], "agg": "count"})
    assert data["columns"] == ["地区", "count"]
    got = {row["地区"]: row["count"] for row in data["rows"]}
    assert got == REGION_COUNTS
    assert sum(got.values()) == len(df)


def test_group_aggregate_limit_and_sort_order(df: pd.DataFrame) -> None:
    desc = call(
        df,
        "group_aggregate",
        {"group_by": ["地区"], "metric": "销售额", "agg": "mean", "limit": 2},
    )
    assert [row["地区"] for row in desc["rows"]] == ["华南", "东北"]
    assert desc["rows"][0]["销售额_mean"] == pytest.approx(REGION_MEANS["华南"])
    assert desc["group_count"] == 6
    assert desc["row_count"] == 2

    asc = call(
        df,
        "group_aggregate",
        {"group_by": ["地区"], "metric": "销售额", "agg": "mean", "limit": 2, "sort_order": "asc"},
    )
    assert [row["地区"] for row in asc["rows"]] == ["西北", "西南"]


def test_group_aggregate_two_group_columns(df: pd.DataFrame) -> None:
    data = call(df, "group_aggregate", {"group_by": ["地区", "类别"], "metric": "销售额", "agg": "sum"})
    assert data["columns"] == ["地区", "类别", "销售额_sum"]
    assert data["group_count"] == df.groupby(by=["地区", "类别"], dropna=False, observed=True).ngroups
    total = sum(row["销售额_sum"] for row in data["rows"])
    assert total == pytest.approx(float(df["销售额"].sum()))


def test_group_aggregate_can_sort_by_a_group_column(df: pd.DataFrame) -> None:
    data = call(
        df,
        "group_aggregate",
        {"group_by": ["地区"], "metric": "销售额", "agg": "sum", "sort_by": "地区", "sort_order": "asc"},
    )
    regions = [row["地区"] for row in data["rows"]]
    assert regions == sorted(regions)


def test_group_aggregate_median_matches_pandas(df: pd.DataFrame) -> None:
    data = call(df, "group_aggregate", {"group_by": ["类别"], "metric": "利润", "agg": "median"})
    expected = df.groupby(by=["类别"], as_index=False, dropna=False, observed=True)["利润"].agg("median")
    got = {row["类别"]: row["利润_median"] for row in data["rows"]}
    for row in expected.itertuples(index=False):
        assert got[row[0]] == pytest.approx(float(row[1]))


def test_group_aggregate_rejects_sum_without_metric(df: pd.DataFrame) -> None:
    message = expect_code(
        df, "group_aggregate", {"group_by": ["地区"], "agg": "sum"}, "BAD_OPERATION"
    )
    assert "metric" in message


def test_group_aggregate_rejects_duplicate_group_columns(df: pd.DataFrame) -> None:
    message = expect_code(
        df,
        "group_aggregate",
        {"group_by": ["地区", "地区"], "metric": "销售额", "agg": "sum"},
        "BAD_OPERATION",
    )
    assert "重复" in message


def test_group_aggregate_rejects_a_text_metric(df: pd.DataFrame) -> None:
    message = expect_code(
        df, "group_aggregate", {"group_by": ["地区"], "metric": "商品", "agg": "sum"}, "BAD_OPERATION"
    )
    assert "不是数值列" in message


def test_group_aggregate_rejects_too_many_group_columns(df: pd.DataFrame) -> None:
    message = expect_code(
        df,
        "group_aggregate",
        {"group_by": ["地区", "类别", "商品", "日期"], "agg": "count"},
        "BAD_PARAMS",
    )
    assert "最多 3 项" in message


def test_group_aggregate_rejects_an_unknown_param(df: pd.DataFrame) -> None:
    message = expect_code(
        df,
        "group_aggregate",
        {"group_by": ["地区"], "metric": "销售额", "agg": "sum", "sort": "销售额"},
        "BAD_PARAMS",
    )
    assert "sort_by" in message  # 消息要告诉模型正确的写法


# ===================================================================== top_n


def test_top_n_returns_the_three_biggest_sales(df: pd.DataFrame) -> None:
    data = call(df, "top_n", {"by": "销售额", "n": 3})
    assert data["row_count"] == 3
    assert [row["销售额"] for row in data["rows"]] == pytest.approx([14999850.0, 8599914.0, 1028560.0])
    assert data["columns"] == ["销售额"]


def test_top_n_matches_pandas_nlargest(df: pd.DataFrame) -> None:
    data = call(df, "top_n", {"by": "销售额", "n": 5, "columns": ["商品", "地区", "销售额"]})
    expected = df.nlargest(5, "销售额")[["商品", "地区", "销售额"]]
    assert [row["商品"] for row in data["rows"]] == list(expected["商品"])
    assert [row["销售额"] for row in data["rows"]] == pytest.approx(list(expected["销售额"]))


def test_top_n_ascending_returns_the_smallest(df: pd.DataFrame) -> None:
    data = call(df, "top_n", {"by": "销售额", "n": 3, "ascending": True})
    values = [row["销售额"] for row in data["rows"]]
    assert values == sorted(values)
    assert values[0] == pytest.approx(float(df["销售额"].min()))


def test_top_n_per_group_returns_one_row_per_region(df: pd.DataFrame) -> None:
    data = call(df, "top_n", {"by": "销售额", "n": 1, "group_by": ["地区"]})
    assert data["row_count"] == 6
    got = {row["地区"]: row["销售额"] for row in data["rows"]}
    expected = df.loc[df.groupby("地区")["销售额"].idxmax()]
    for row in expected.itertuples(index=False):
        assert got[row.地区] == pytest.approx(float(row.销售额))


def test_top_n_rejects_a_text_sort_column(df: pd.DataFrame) -> None:
    message = expect_code(df, "top_n", {"by": "商品", "n": 3}, "BAD_OPERATION")
    assert "不是数值列" in message


def test_top_n_rejects_out_of_range_n(df: pd.DataFrame) -> None:
    assert "不能小于 1" in expect_code(df, "top_n", {"by": "销售额", "n": 0}, "BAD_PARAMS")
    assert "不能大于 50" in expect_code(df, "top_n", {"by": "销售额", "n": 51}, "BAD_PARAMS")


def test_top_n_rejects_a_bool_for_n(df: pd.DataFrame) -> None:
    """Python 里 True 是 int 的子类，不挡的话 n=True 会被当成 n=1 收下。"""
    message = expect_code(df, "top_n", {"by": "销售额", "n": True}, "BAD_PARAMS")
    assert "类型不对" in message


# =========================================================== compare_groups


def test_compare_groups_means_and_differences_match_hand_computation(df: pd.DataFrame) -> None:
    data = call(df, "compare_groups", {"group_by": "地区", "metric": "销售额"})
    assert data["columns"] == ["地区", "销售额_mean", "差值"]
    assert data["row_count"] == 6
    got = {row["地区"]: row["销售额_mean"] for row in data["rows"]}
    assert got == pytest.approx(REGION_MEANS)
    # 差值 = 本组均值 - 各组均值的平均
    overall = sum(REGION_MEANS.values()) / len(REGION_MEANS)
    for row in data["rows"]:
        assert row["差值"] == pytest.approx(round(row["销售额_mean"] - overall, 4))


def test_compare_groups_top_k_limits_the_rows(df: pd.DataFrame) -> None:
    data = call(df, "compare_groups", {"group_by": "地区", "metric": "销售额", "top_k": 2})
    assert data["row_count"] == 2
    assert [row["地区"] for row in data["rows"]] == ["华南", "东北"]


def test_compare_groups_count_ignores_missing_values(df: pd.DataFrame) -> None:
    """`count` 数的是**非空值**：销量有 18 个空值，所以各地区计数比总行数少几个。

    （要数总行数得用 `group_aggregate` 的 `agg="count"` 且不传 metric —— 那条路走的是
    `groupby.size()`，两种 "count" 在 pandas 里本来就不同，这里把它钉住。）
    """
    data = call(df, "compare_groups", {"group_by": "地区", "metric": "销量", "agg": "count"})
    got = {row["地区"]: row["销量_count"] for row in data["rows"]}
    assert got == df.groupby("地区")["销量"].count().to_dict()
    assert sum(got.values()) == int(df["销量"].count()) == 2150
    assert sum(got.values()) < len(df)


def test_compare_groups_rejects_a_text_metric(df: pd.DataFrame) -> None:
    message = expect_code(
        df, "compare_groups", {"group_by": "地区", "metric": "商品"}, "BAD_OPERATION"
    )
    assert "不是数值列" in message


# ============================================================== correlation


def test_correlation_sales_and_profit(df: pd.DataFrame) -> None:
    data = call(df, "correlation", {"columns": ["销售额", "利润"]})
    assert data["columns"] == ["销售额", "利润"]
    assert data["matrix"] == [[1.0, 0.9872], [0.9872, 1.0]]
    assert data["pairs"] == [{"x": "销售额", "y": "利润", "r": 0.9872}]


def test_correlation_defaults_to_all_numeric_columns(df: pd.DataFrame) -> None:
    data = call(df, "correlation", {})
    assert data["columns"] == ["销量", "单价", "销售额", "利润", "客户评分"]
    assert len(data["matrix"]) == 5
    assert len(data["pairs"]) == 10  # 5 列两两组合
    assert data["pairs"][0] == {"x": "销量", "y": "单价", "r": -0.0457}


def test_correlation_pairs_agree_with_the_matrix(df: pd.DataFrame) -> None:
    data = call(df, "correlation", {})
    index = {name: position for position, name in enumerate(data["columns"])}
    for pair in data["pairs"]:
        row = index[pair["x"]]
        column = index[pair["y"]]
        assert pair["r"] == pytest.approx(data["matrix"][row][column])
        assert data["matrix"][row][row] == 1.0


def test_correlation_rejects_a_text_column(df: pd.DataFrame) -> None:
    message = expect_code(df, "correlation", {"columns": ["销售额", "地区"]}, "BAD_OPERATION")
    assert "不是数值列" in message


def test_correlation_returns_empty_pairs_for_a_constant_column() -> None:
    """常量列的标准差是 0，相关系数算不出来 → NaN → 必须清洗成 None 而不是漏出去。"""
    frame = pd.DataFrame({"常量": [1.0, 1.0, 1.0], "变化": [1.0, 2.0, 3.0]})
    data = call(frame, "correlation", {})
    assert data["pairs"][0]["r"] is None
    assert data["matrix"][0][1] is None


# ======================================================= time_series_summary


def test_time_series_monthly_has_twelve_rows(df: pd.DataFrame) -> None:
    """要求 8：2024 全年按月的 12 行。"""
    data = call(df, "time_series_summary", {"date_column": "日期", "metric": "销售额", "freq": "M"})
    assert data["row_count"] == 12
    assert data["columns"] == ["period", "销售额_sum"]
    assert [row["period"] for row in data["rows"]] == list(MONTHLY_SUMS)


def test_time_series_monthly_sums_match_hand_computation(df: pd.DataFrame) -> None:
    data = call(df, "time_series_summary", {"date_column": "日期", "metric": "销售额", "freq": "M"})
    got = {row["period"]: row["销售额_sum"] for row in data["rows"]}
    assert got == pytest.approx(MONTHLY_SUMS)


def test_time_series_skips_rows_with_unparseable_dates(df: pd.DataFrame) -> None:
    """24 行日期是空的：它们不进任何一个月，所以月度合计小于全表合计 —— 这是有意行为。"""
    data = call(df, "time_series_summary", {"date_column": "日期", "metric": "销售额", "freq": "M"})
    dates = pd.to_datetime(df["日期"], errors="coerce")
    assert data["skipped_row_count"] == 24
    assert sum(row["销售额_sum"] for row in data["rows"]) == pytest.approx(
        float(df.loc[dates.notna(), "销售额"].sum())
    )
    assert sum(row["销售额_sum"] for row in data["rows"]) < float(df["销售额"].sum())


def test_time_series_growth_starts_with_none_then_numbers(df: pd.DataFrame) -> None:
    """要求 8：第一行环比是 None，后续行是数字（比例，0.1116 = 上涨 11.16%）。"""
    data = call(
        df,
        "time_series_summary",
        {"date_column": "日期", "metric": "销售额", "freq": "M", "include_growth": True},
    )
    assert data["columns"] == ["period", "销售额_sum", "环比"]
    growth = [row["环比"] for row in data["rows"]]
    assert growth[0] is None
    assert all(isinstance(value, float) for value in growth[1:])
    assert growth[1] == pytest.approx(0.1116)
    assert growth[3] == pytest.approx(1.7862)


def test_time_series_week_labels_use_iso_weeks(df: pd.DataFrame) -> None:
    data = call(df, "time_series_summary", {"date_column": "日期", "metric": "销售额", "freq": "W"})
    assert data["row_count"] == 52
    assert data["rows"][0]["period"] == "2024-W01"


def test_time_series_day_frequency(df: pd.DataFrame) -> None:
    data = call(df, "time_series_summary", {"date_column": "日期", "metric": "销售额", "freq": "D"})
    assert data["row_count"] == 336
    assert data["rows"][0]["period"] == "2024-01-01"


def test_time_series_rejects_a_numeric_date_column(df: pd.DataFrame) -> None:
    """pd.to_datetime 会把整数当 1970 年起的纳秒偏移，算出来的"日期"没有意义。"""
    message = expect_code(
        df, "time_series_summary", {"date_column": "销量", "metric": "销售额"}, "BAD_OPERATION"
    )
    assert "不是日期列" in message


def test_time_series_rejects_an_unknown_freq(df: pd.DataFrame) -> None:
    message = expect_code(
        df,
        "time_series_summary",
        {"date_column": "日期", "metric": "销售额", "freq": "Q"},
        "BAD_PARAMS",
    )
    assert "D/W/M" in message


# =============================================================== 校验通用行为


@pytest.mark.parametrize("name", sorted(VALID_CALLS))
def test_every_tool_result_is_json_serializable(df: pd.DataFrame, name: str) -> None:
    """要求 6：每个工具的返回值都要能过 json.dumps(allow_nan=False)。"""
    data = call(df, name, VALID_CALLS[name])
    text = json.dumps(data, allow_nan=False, ensure_ascii=False)
    assert isinstance(text, str)
    assert text


@pytest.mark.parametrize("name", sorted(VALID_CALLS))
def test_no_numpy_scalar_leaks_into_a_result(df: pd.DataFrame, name: str) -> None:
    """跑完真实工具后，结果树里只允许出现 JSON 原生类型。"""

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str), path
                walk(item, f"{path}.{key}")
            return
        if isinstance(value, list):
            for position, item in enumerate(value):
                walk(item, f"{path}[{position}]")
            return
        assert isinstance(value, (int, float, str, bool, type(None))), f"{path} 是 {type(value).__name__}"

    walk(call(df, name, VALID_CALLS[name]), "$")


@pytest.mark.parametrize("name", sorted(BAD_PARAMS_CALLS))
def test_every_tool_reports_bad_params(df: pd.DataFrame, name: str) -> None:
    expect_code(df, name, BAD_PARAMS_CALLS[name], "BAD_PARAMS")


@pytest.mark.parametrize("name", sorted(MISSING_COLUMN_CALLS))
def test_every_tool_reports_a_missing_column_with_real_names(df: pd.DataFrame, name: str) -> None:
    """要求 1（列不存在那档）：消息里必须附上真实列名，模型才有机会改对。"""
    message = expect_code(df, name, MISSING_COLUMN_CALLS[name], "COLUMN_NOT_FOUND")
    assert "真实列名是" in message
    assert "销售额" in message


@pytest.mark.parametrize(
    "name", ["os.system", "eval", "__import__", ""]
)
def test_unknown_tools_are_rejected(df: pd.DataFrame, name: str) -> None:
    """要求 4：白名单外必须是 TOOL_NOT_FOUND，且不会去执行任何东西。"""
    message = expect_code(df, name, {}, "TOOL_NOT_FOUND")
    assert "可用工具" in message


def test_validation_short_circuits_in_order(df: pd.DataFrame) -> None:
    """前面的校验没过，就不要再去查后面的（否则错误消息会指错方向）。"""
    # 工具名错 + 参数也错 → 报告工具名错
    expect_code(df, "os.system", {"nonsense": 1}, "TOOL_NOT_FOUND")
    # 参数错 + 列也不存在 → 报告参数错
    expect_code(df, "group_aggregate", {"group_by": ["不存在的列"]}, "BAD_PARAMS")
    # 列不存在 + 操作也不合法 → 报告列不存在
    expect_code(df, "group_aggregate", {"group_by": ["不存在的列"], "metric": "地区", "agg": "sum"}, "COLUMN_NOT_FOUND")


def test_column_list_in_the_message_is_capped_at_twenty() -> None:
    wide = pd.DataFrame({f"列{i}": [1, 2, 3] for i in range(30)})
    message = expect_code(wide, "describe_dataset", {"columns": ["不存在"]}, "COLUMN_NOT_FOUND")
    assert "还有 10 列没列出来，一共 30 列" in message


def test_tool_validation_error_carries_code_message_and_detail(df: pd.DataFrame) -> None:
    with pytest.raises(ToolValidationError) as excinfo:
        validate_call("group_aggregate", {"group_by": ["城市"], "agg": "count"}, df)
    error = excinfo.value
    assert error.error_code == "COLUMN_NOT_FOUND"
    assert "城市" in error.error_message
    assert error.detail == "数据集一共 9 列"
    assert str(error) == error.error_message


# ------------------------------------------------- 空列表参数：拒掉，但别拒过头


def test_empty_column_list_is_rejected_for_every_tool(df: pd.DataFrame) -> None:
    """`columns: []` / `group_by: []` 必须是 BAD_PARAMS，不能"成功但空"。

    为什么这条重要：模型给出的参数里出现空数组是完全现实的（它想表达"我不限制"）。
    如果放行，工具会返回 `{"columns": [], "stats": []}` 且 `ok=true` —— 一个**看着成功、
    实则什么都没算**的结果会一路流进报告，而报告里"该有表格的地方是空的"很难被当成错误
    发现（前端不会报错、日志里没有异常）。这类"看起来能跑、语义错了"正是本项目最防的失败。
    """
    empty_list_calls = {
        "describe_dataset": {"columns": []},
        "group_aggregate": {"group_by": [], "metric": "销售额", "agg": "sum"},
        "top_n": {"by": "销售额", "n": 5, "columns": []},
        "missing_values": {"columns": []},
        "correlation": {"columns": []},
    }
    for name, params in empty_list_calls.items():
        message = expect_code(df, name, params, "BAD_PARAMS")
        assert "至少要" in message, f"{name} 的消息没说清下限：{message}"


@pytest.mark.parametrize("name", ["describe_dataset", "missing_values", "correlation"])
def test_omitting_the_column_list_still_means_all_columns(df: pd.DataFrame, name: str) -> None:
    """上一条的对照组：**不传** `columns` 必须仍然成功，且真的覆盖了全部相关列。

    两条测试必须一起看。只测"空列表被拒"的话，一个"把 `columns` 一律拒掉"的坏实现
    照样能过——而那个坏实现会让模型永远无法使用默认行为。
    """
    data = call(df, name, {})
    if name == "correlation":
        # 默认 = 全部数值列（销售额、利润、销量、单价、客户评分）
        assert data["columns"] == ["销量", "单价", "销售额", "利润", "客户评分"]
    elif name == "describe_dataset":
        assert data["columns"] == ["销量", "单价", "销售额", "利润", "客户评分"]
        assert len(data["stats"]) == 5
    else:
        # missing_values 默认看全部 9 列（含文本列）
        assert len(data["columns"]) == 9
        assert {item["column"] for item in data["columns"]} == set(df.columns)


def test_min_items_appears_in_the_schema_sent_to_the_model(df: pd.DataFrame) -> None:
    """`min_items` 必须出现在 `to_openai_tools()` 里，不能只活在校验器内部。

    理由：校验器拦住空数组只是"事后拒绝"；把 `minItems` 写进发给模型的 JSON Schema，
    模型**一开始就不会生成**空数组。少一轮"失败 → 重试"就是少一次 API 调用。
    """
    tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in to_openai_tools()}
    assert tools["describe_dataset"]["properties"]["columns"]["minItems"] == 1
    assert tools["missing_values"]["properties"]["columns"]["minItems"] == 1
    assert tools["correlation"]["properties"]["columns"]["minItems"] == 2
    assert tools["group_aggregate"]["properties"]["group_by"]["minItems"] == 1
    assert tools["group_aggregate"]["properties"]["group_by"]["maxItems"] == 3



# =============================================================== jsonable


def test_to_jsonable_converts_pandas_and_numpy_values() -> None:
    converted = to_jsonable(
        {
            "整数": np.int64(3),
            "浮点": np.float64(1.5),
            "布尔": np.bool_(True),
            "缺失": np.nan,
            "无穷": float("inf"),
            "日期": pd.Timestamp("2024-01-05"),
            "标量缺失": pd.NaT,
            "列表": [np.int64(1), {"里面": np.float64("nan")}],
        }
    )
    assert converted == {
        "整数": 3,
        "浮点": 1.5,
        "布尔": True,
        "缺失": None,
        "无穷": None,
        "日期": "2024-01-05T00:00:00",
        "标量缺失": None,
        "列表": [1, {"里面": None}],
    }
    assert isinstance(converted["整数"], int)
    assert isinstance(converted["布尔"], bool)


@pytest.mark.parametrize(
    "bad",
    [
        {"x": np.float64(1.0)},
        {"x": np.int64(3)},
        {"x": np.bool_(True)},
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": pd.Timestamp("2024-01-05")},
        {"x": [1, {"y": np.int64(2)}]},
    ],
)
def test_ensure_jsonable_rejects_anything_not_json_native(bad: dict[str, Any]) -> None:
    """校验 5 的裁判：工具忘了清洗，必须在这里当场抓住（BAD_RESULT）。"""
    with pytest.raises(ToolValidationError) as excinfo:
        ensure_jsonable(bad)
    assert excinfo.value.error_code == "BAD_RESULT"


def test_ensure_jsonable_accepts_a_cleaned_result() -> None:
    data = {"rows": [{"地区": "华东", "销售额_sum": 120345.5, "环比": None}]}
    assert ensure_jsonable(data) is data
