"""app/data/profiler.py 的测试。

这个模块是"AI 分析"的地基：画像里的数字错了，后面所有结论都是错的。
所以这里的测试都在做同一件事 —— **拿 pandas 手工算一遍，和 profiler 的输出对数**，
而不是硬编码一堆"看起来对"的数字。

还有一类测试专门盯"能不能安全地变成 JSON"：接口最终要 json.dumps 这个画像，
NaN、np.int64、Timestamp 这些东西漏出去就是 500。
"""

import json

import pandas as pd

from app.data.profiler import build_profile


def _columns(profile):
    """把 columns 列表变成 {列名: ColumnProfile}，方便按名字取。"""
    return {column.name: column for column in profile.columns}


def _dump(profile) -> str:
    """按接口真实的方式序列化一遍，任何不合法的东西都会在这里炸出来。"""
    return json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)


def test_semantic_types_of_typical_columns():
    df = pd.DataFrame(
        {
            "销售额": [100.0, 200.0, 300.0, 400.0],
            "地区": ["华东", "华北", "华东", "华南"],
            "日期": ["2024-01-05", "2024-02-06", "2024-03-07", "2024-04-08"],
            "是否成交": [True, False, True, True],
            "备注": [
                "客户要求周末送达，麻烦提前联系配送员",
                "第二次回购，需要开增值税专用发票",
                "地址写错了一次，后来打电话改过来了",
                "需要开发票并且附上纸质的对账单",
            ],
        }
    )

    profile = build_profile(df, "d1")

    kinds = {column.name: column.semantic_type for column in profile.columns}
    assert kinds == {
        "销售额": "numeric",
        "地区": "categorical",
        "日期": "datetime",
        "是否成交": "boolean",
        "备注": "text",
    }
    assert profile.dataset_id == "d1"
    assert profile.row_count == 4
    assert profile.column_count == 5
    assert profile.duplicate_row_count == 0
    assert profile.memory_bytes > 0


def test_numeric_stats_match_pandas_describe():
    df = pd.DataFrame({"金额": [10.0, 20.0, 30.0, 40.0, 50.0]})
    describe = df["金额"].describe()

    column = _columns(build_profile(df, "d1"))["金额"]

    assert column.dtype == "float64"
    assert column.min_value == describe["min"]
    assert column.max_value == describe["max"]
    assert column.mean_value == describe["mean"]
    assert column.std_value == describe["std"]  # pandas 默认 ddof=1（样本标准差）
    assert column.median_value == describe["50%"]
    assert column.q1_value == describe["25%"]
    assert column.q3_value == describe["75%"]
    assert column.null_count == 0
    assert column.null_ratio == 0.0
    assert column.unique_count == 5
    # 数字必须是纯 Python 类型：np.float64 虽然也是 float 的子类，但 np.int64 不是 int。
    assert type(column.min_value) is float
    assert type(column.null_count) is int


def test_single_value_column_has_no_std_but_is_still_json_safe():
    df = pd.DataFrame({"数量": [5]})

    column = _columns(build_profile(df, "d1"))["数量"]

    assert column.mean_value == 5.0
    assert column.std_value is None  # 一个值算不出标准差，是"算不出来"，不是 0
    _dump(build_profile(df, "d1"))  # NaN 漏出去的话这里会抛 ValueError


def test_missing_values_are_counted_and_warned():
    df = pd.DataFrame({"金额": [1.0, None, 3.0, None, 5.0]})

    profile = build_profile(df, "d1")
    column = _columns(profile)["金额"]

    assert column.null_count == 2
    assert column.null_ratio == 0.4
    assert column.unique_count == 3  # 缺失值不算唯一值
    assert column.mean_value == 3.0  # pandas 默认跳过缺失值算均值
    assert any("缺失 40.00%" in warning for warning in profile.warnings)


def test_all_null_column_is_warned_and_has_no_stats():
    df = pd.DataFrame({"空列": [None, None, None]})

    profile = build_profile(df, "d1")
    column = _columns(profile)["空列"]

    assert column.null_count == 3
    assert column.null_ratio == 1.0
    assert column.min_value is None
    assert column.mean_value is None
    assert column.std_value is None
    assert column.top_values == []
    assert any("全部为空" in warning for warning in profile.warnings)
    _dump(profile)


def test_duplicate_rows_are_counted_and_warned():
    df = pd.DataFrame({"编号": [1, 1, 2], "名称": ["x", "x", "y"]})

    profile = build_profile(df, "d1")

    assert profile.duplicate_row_count == 1
    assert any("完全重复" in warning for warning in profile.warnings)


def test_datetime_column_only_reports_min_and_max_as_iso_strings():
    df = pd.DataFrame({"日期": ["2024-03-07", "2024-01-05", "2024-02-06"]})

    column = _columns(build_profile(df, "d1"))["日期"]

    assert column.semantic_type == "datetime"
    assert column.min_value == "2024-01-05T00:00:00"
    assert column.max_value == "2024-03-07T00:00:00"
    assert column.mean_value is None  # "平均日期"没有业务意义，不给
    _dump(build_profile(df, "d1"))  # Timestamp 漏出去的话序列化会失败


def test_datetime_column_with_unparseable_values_warns_about_them():
    df = pd.DataFrame({"日期": ["2024-01-05", "2024-02-06", "2024-03-07", "2024-04-08", "缺"]})

    profile = build_profile(df, "d1")
    column = _columns(profile)["日期"]

    # 4/5 = 80% 能解析 → 仍然认它是日期列，但要把那 1 个坏值说清楚
    assert column.semantic_type == "datetime"
    assert any("有 1 个值无法解析为日期" in warning for warning in profile.warnings)


def test_native_datetime_column_with_blanks_is_not_called_unparseable():
    """原生 datetime64 列里的空单元格是**缺失值**，不是"解析失败"。

    这是 QA 在真实 .xlsx 上端到端复现出来的假话：Excel 日期列 5 格其中 2 格空白，
    列本来就是 `datetime64[us]`、2 个 NaT、0 个解析失败，画像却输出
    "有 2 个值无法解析为日期"。根因是 `_classify` 把**带 NaT 的原始列**当成
    "我们解析出来的日期列"传下去，于是 `parsed_dates.isna().sum()` 数出来的
    其实是缺失值个数。

    为什么这条必须钉住：画像里出现一个假数字（或一句假话）就是阻塞 —— 用户会
    以为那两个格子里填了看不懂的东西，跑去检查源文件，而实际上只是空着。
    """
    df = pd.DataFrame(
        {
            "日期": pd.to_datetime(["2024-01-05", "2024-02-01", None, None, "2024-03-01"]),
            "销售额": [100, 200, 300, 400, 500],
        }
    )

    profile = build_profile(df, "d1")
    column = _columns(profile)["日期"]

    assert column.semantic_type == "datetime"
    assert column.null_count == 2
    # 缺失值该说的还是要说
    assert any("缺失 40.00%" in warning for warning in profile.warnings)
    # 但不能说成"无法解析"
    assert not any("无法解析为日期" in warning for warning in profile.warnings)
    # 最早的日期仍要正确（走的是 non_null 那条分支）
    assert column.min_value == "2024-01-05T00:00:00"
    assert column.max_value == "2024-03-01T00:00:00"


def test_csv_date_column_with_blanks_is_not_called_unparseable_either():
    """字符串日期列里的空值同理：那是缺失值，不是解析失败。

    对照 `test_datetime_column_with_unparseable_values_warns_about_them`：
    那条测的是**真的填了看不懂的值**（"缺"），这条测的是**格子空着**。
    两条一起才说明"空值"与"坏值"没有被混为一谈。

    这里用 `None` 而不是 `""` 表示空格，是**刻意的**：CSV 里的空字段经 pandas
    读进来就是 NaN（实测 `null_count=2`），loader 交给 profiler 的就是这个形态。
    直接塞 `""` 会得到一个装着两个空字符串的**字符串列**（5 个值里 3 个能解析，
    60% < 80% 阈值 → 被判成 categorical），那是另一个场景，不是本条要钉的东西。
    """
    df = pd.DataFrame({"日期": ["2024-01-05", "2024-02-01", None, None, "2024-03-01"]})

    profile = build_profile(df, "d1")
    column = _columns(profile)["日期"]

    assert column.semantic_type == "datetime"
    assert column.null_count == 2
    assert any("缺失 40.00%" in warning for warning in profile.warnings)
    assert not any("无法解析为日期" in warning for warning in profile.warnings)


def test_digit_only_text_is_not_mistaken_for_dates():
    # 纯数字列走 pd.to_datetime 会被当成 1970 年起的纳秒偏移（编号 1 会变成
    # 1970-01-01 00:00:00.000000001）。这是实测过的坑，所以必须有"数字样"守卫。
    df = pd.DataFrame({"编号": ["1", "2", "3", "4"]})

    column = _columns(build_profile(df, "d1"))["编号"]

    assert column.semantic_type == "categorical"
    assert column.min_value is None


def test_thousand_separator_text_is_flagged_as_text_holding_numbers():
    df = pd.DataFrame({"单价": ["1,200", "3,400", "5,600", "7,800"]})

    profile = build_profile(df, "d1")
    column = _columns(profile)["单价"]

    assert column.semantic_type == "categorical"
    assert any("被识别为文本" in warning for warning in profile.warnings)


def test_boolean_column_is_not_counted_as_numeric():
    df = pd.DataFrame({"是否成交": [True, False, True]})

    column = _columns(build_profile(df, "d1"))["是否成交"]

    assert column.semantic_type == "boolean"
    assert column.mean_value is None  # 不能出现"平均布尔值 0.67"这种统计
    assert column.top_values[0] == {"value": "True", "count": 2}


def test_categorical_column_reports_top_values_and_samples():
    df = pd.DataFrame({"地区": ["华东", "华东", "华北", "华东"]})

    column = _columns(build_profile(df, "d1"))["地区"]

    assert column.semantic_type == "categorical"
    assert column.unique_count == 2  # 只有"华东 / 华北"两个不同值（华东出现 3 次）
    assert column.top_values == [
        {"value": "华东", "count": 3},
        {"value": "华北", "count": 1},
    ]
    assert column.sample_values == ["华东", "华东", "华北"]


def test_high_cardinality_column_skips_top_values():
    # 唯一值超过 1 万就不再统计高频值（docs/ARCHITECTURE.md §13 风险 7）：
    # 完整计数一遍又慢、结论又没意义。样例值还是照给。
    df = pd.DataFrame({"订单号": [f"NO{index}" for index in range(10_001)]})

    column = _columns(build_profile(df, "d1"))["订单号"]

    assert column.unique_count == 10_001
    assert column.top_values == []
    assert len(column.sample_values) == 3


def test_profile_of_mixed_table_is_fully_json_serializable():
    df = pd.DataFrame(
        {
            "金额": [1.5, None, 3.25],
            "日期": ["2024-01-05", None, "2024-03-07"],
            "地区": ["华东", None, "华南"],
            "是否有效": [True, None, False],
        }
    )

    profile = build_profile(df, "mixed")
    text = _dump(profile)

    assert '"semantic_type"' in text
    assert len(profile.columns) == 4
