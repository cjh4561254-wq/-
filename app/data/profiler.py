"""数据画像：用 pandas 真实算出"这份数据长什么样"。

这个模块负责什么
----------------
1. 逐列给出：dtype、语义类型（numeric / categorical / datetime / boolean / text）、
   缺失个数与比例、唯一值个数、五数概括（min / q1 / median / q3 / max）、
   均值与标准差、出现最多的几个值、几个样例值。
2. 给出整体信息：总行数、总列数、完全重复的行数、内存占用。
3. 把"这列有问题"翻译成中文人话，攒进 `warnings`。

这个模块不负责什么
------------------
- **不调 LLM、不猜业务含义、不编造任何数字**：所有数字都是 pandas 算出来的，
  算不出来的一律是 `None`（不是 0，也不是 NaN）。这一条是硬要求 ——
  画像里出现一个假数字，后面所有"AI 分析"都会建立在假数字上。
- 不读文件（`app/data/loader.py` 的事）、不落盘、不记 id（`app/data/store.py` 的事）。
- 不做清洗：画像只描述现状，改数据是用户看了画像之后自己决定的事。

返回值必须"能进 JSON"
----------------------
`DatasetProfile` 最终会被 `json.dumps` 成接口响应，所以：

- 所有数字都转成纯 Python 的 `float` / `int`（`np.float64` 是 float 的子类还好，
  但 `np.int64` **不是** int，`np.bool_` 也不是 bool）；
- NaN / Infinity 一律变成 `None` —— JSON 里没有 NaN 这两个字面量，
  `json.dumps(..., allow_nan=False)` 会直接抛 `ValueError`；
- 日期统一输出 ISO 字符串（`2024-01-05T00:00:00`），不让 `Timestamp` 漏出去。
"""

import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.schemas_dataset import ColumnProfile, DatasetProfile

# 缺失比例超过这个数就给警告。5% 是个经验值：再低的话几乎每列都会报警，
# 报警太多等于没报警（用户会全部忽略）。
NULL_WARNING_RATIO = 0.05

# 文本列里"能当数字用"的比例达到这个数，就提示"这列其实装着数字"。
NUMERIC_LIKE_RATIO = 0.8

# 试出来的日期值占比达到这个数，才认这一列是日期列。
# 为什么是 0.8 而不是 1.0：真实数据里"2024-01-05 / 2024-01-06 / 无"混在一起很常见，
# 少数几个填错的值不该让整列丢掉日期类型。
DATETIME_SUCCESS_RATIO = 0.8

# 日期粗筛时最多看几个值（抽样，不扫全列）。
DATETIME_SAMPLE_SIZE = 200

# 判断"文本"的两个门槛：不同值占比、平均字符长度。
# 两者都要满足：只看唯一性的话，订单号也会被当成文本。
TEXT_UNIQUE_RATIO = 0.8
TEXT_MIN_AVERAGE_LENGTH = 12

# 每列最多给几个"出现最多的值"和几个样例值。
TOP_VALUES_LIMIT = 3
SAMPLE_VALUES_LIMIT = 3

# 唯一值超过这个数的列不再统计 top_values：value_counts 要把整列完整计数一遍，
# 对一列几十万行的备注文本既慢又没意义（那份"最高频值"本来就只出现一两次）。
# docs/ARCHITECTURE.md §13 风险 7 提到的就是这个取舍。
TOP_VALUES_CARDINALITY_LIMIT = 10_000


def build_profile(df: pd.DataFrame, dataset_id: str) -> DatasetProfile:
    """算出整份数据的画像。

    参数
    ----
    df : loader 清洗过、**列名唯一**的 DataFrame。列名唯一这条很关键：
         下面的统计是按列名取列的，重名列会让 `df[name]` 返回一个 DataFrame
         而不是一列。loader 已经保证了这一点（见 `_make_unique_columns`）。
    dataset_id : 回填给前端，用来和数据集对上号。

    返回
    ----
    `DatasetProfile`。它里面没有 DataFrame，都是能被 JSON 序列化的纯数据。
    """
    warnings: list[str] = []

    columns = [_profile_column(df[name], warnings) for name in df.columns]

    row_count = int(df.shape[0])
    duplicate_row_count = int(df.duplicated().sum())
    if duplicate_row_count > 0:
        warnings.append(
            f"检测到 {duplicate_row_count} 行完全重复的记录，"
            f"去重后还剩 {row_count - duplicate_row_count} 行"
        )

    return DatasetProfile(
        dataset_id=dataset_id,
        row_count=row_count,
        column_count=int(df.shape[1]),
        columns=columns,
        duplicate_row_count=duplicate_row_count,
        # deep=True：把字符串/对象列真正指向的那块内存也算进来，
        # 否则一列中文文本只会被算成几个指针的大小（几十字节），完全是错的。
        # 这个数字含 index 本身占用，作为"这份数据大概占多少内存"足够用了。
        memory_bytes=int(df.memory_usage(deep=True).sum()),
        warnings=warnings,
        built_at=datetime.now(timezone.utc),
    )


def _profile_column(column: pd.Series, warnings: list[str]) -> ColumnProfile:
    """算一列的画像，并把这一列的问题写进 `warnings`。"""
    name = str(column.name)
    row_count = int(len(column))
    non_null = column.dropna()
    null_count = int(column.isna().sum())
    # 比例保留 4 位小数（schema 里就是这么约定的）：0.1235 表示 12.35%。
    # round 在这里是"接口契约"而不是"美化"—— 前端拿它直接乘 100 显示。
    null_ratio = round(null_count / row_count, 4) if row_count else 0.0
    unique_count = int(column.nunique())

    semantic_type, parsed_dates = _classify(column)

    min_value: float | str | None = None
    max_value: float | str | None = None
    mean_value: float | None = None
    std_value: float | None = None
    median_value: float | None = None
    q1_value: float | None = None
    q3_value: float | None = None
    top_values: list[dict[str, Any]] = []
    sample_values: list[str] = []

    if semantic_type == "numeric":
        # 数值列：五数概括 + 均值 + 标准差。
        # 少了谁不要紧 —— _to_float 会把 NaN 变成 None
        # （只有 1 个值的列 std 就是 NaN，这不是错误，是"算不出来"）。
        min_value = _to_float(non_null.min())
        max_value = _to_float(non_null.max())
        mean_value = _to_float(non_null.mean())
        std_value = _to_float(non_null.std())  # pandas 默认 ddof=1（样本标准差）
        median_value = _to_float(non_null.median())
        q1_value = _to_float(non_null.quantile(0.25))
        q3_value = _to_float(non_null.quantile(0.75))
    elif semantic_type == "datetime":
        # 日期列只给最早/最晚：算"平均日期"没有业务意义。
        # 统一输出 ISO 字符串，绝不让 Timestamp 漏进模型。
        dates = parsed_dates.dropna() if parsed_dates is not None else non_null
        if len(dates) > 0:
            min_value = dates.min().isoformat()
            max_value = dates.max().isoformat()
    else:
        # 分类 / 文本 / 布尔：给高频值和样例，让用户一眼看出这列装的是什么。
        top_values = _top_values(non_null, unique_count)
        sample_values = [str(value) for value in non_null.head(SAMPLE_VALUES_LIMIT)]

    _collect_warnings(name, column, semantic_type, null_ratio, parsed_dates, warnings)

    return ColumnProfile(
        name=name,
        dtype=str(column.dtype),
        semantic_type=semantic_type,
        null_count=null_count,
        null_ratio=null_ratio,
        unique_count=unique_count,
        min_value=min_value,
        max_value=max_value,
        mean_value=mean_value,
        std_value=std_value,
        median_value=median_value,
        q1_value=q1_value,
        q3_value=q3_value,
        top_values=top_values,
        sample_values=sample_values,
    )


def _classify(column: pd.Series) -> tuple[str, pd.Series | None]:
    """判断一列的语义类型，返回 `(semantic_type, 解析后的日期列或 None)`。

    规则从上往下走，命中就停：

    1. dtype 是 bool → `boolean`。**必须在数值之前判断**：
       pandas 里 True/False 也是数字，不拦住的话布尔列会被算成 numeric，
       然后出现"平均布尔值 0.5"这种没有意义的统计。
    2. dtype 已经是 datetime64 → `datetime`。
    3. dtype 是数值（int / float）→ `numeric`。
    4. 剩下的都当"字符串列"看，再分三步：
       a. 如果这些文本**基本是数字**（≥80% 能转成数字，允许千分位逗号），
          直接归到文本/分类，**不去试日期**。因为 `pd.to_datetime` 会把纯数字
          当成 1970 年起的纳秒偏移：编号列 1、2、3 会被解析成
          `1970-01-01 00:00:00.000000001`。这是实测出来的坑，不是理论担心。
       b. 否则用 `pd.to_datetime(errors="coerce")` 逐值试，非空值里成功比例
          ≥ 80% → `datetime`。
       c. 剩下的看"不同值占比 ≥ 80% 且平均长度 ≥ 12"：是 → `text`（备注、地址、
          商品标题这类），否 → `categorical`。

    为什么判断日期**不看 dtype 猜**：CSV 里的日期天生就是字符串，
    靠 dtype 永远猜不出来；只能真的试着解析，再按成功率下结论。

    已知限制（写出来免得以后有人以为是 bug）：`2024年1月5日` 这种中文日期
    pandas 解析不出来，会被归到文本/分类；`20240105` 这种紧凑数字日期会被
    当成数字文本。两者都会在 warnings 里被提示"这列是文本"。
    """
    if pd.api.types.is_bool_dtype(column):
        return "boolean", None
    if pd.api.types.is_datetime64_any_dtype(column):
        # 第二个返回值是"我们替用户解析出来的日期列"，**只给字符串列用**。
        # 原生 datetime64 列（Excel 日期列最常见）本来就是日期，没有什么可解析的，
        # 所以这里返回 None 而不是 column：否则 `_collect_warnings` 会把列里的
        # NaT（空单元格）当成"解析失败"，报出"有 2 个值无法解析为日期"这种假话 ——
        # 同一件事在原生的缺失值提示里已经报过一次了。
        return "datetime", None
    if pd.api.types.is_numeric_dtype(column):
        return "numeric", None

    non_null = column.dropna()
    if len(non_null) == 0:
        # 一列全是空的：没有类型可判断，归到分类最不容易误导。
        return "categorical", None

    if _numeric_like_ratio(non_null) >= NUMERIC_LIKE_RATIO:
        return _text_or_categorical(non_null), None

    parsed = _try_parse_dates(non_null)
    if parsed is not None:
        return "datetime", parsed

    return _text_or_categorical(non_null), None


def _text_or_categorical(non_null: pd.Series) -> str:
    """文本还是分类？看"不同值的比例"和"平均长度"。

    只有少数几个值反复出现（华东 / 华北 / 华南）是**分类**；
    几乎每个值都不一样、而且都比较长（备注、地址、商品标题）才是**文本**。
    两个条件都要满足 —— 光看唯一性的话，一列订单号也会被算成文本。
    """
    total = len(non_null)
    if total == 0:
        return "categorical"

    unique_ratio = non_null.nunique() / total
    average_length = float(non_null.astype("string").str.len().mean())

    if unique_ratio >= TEXT_UNIQUE_RATIO and average_length >= TEXT_MIN_AVERAGE_LENGTH:
        return "text"
    return "categorical"


def _numeric_like_ratio(non_null: pd.Series) -> float:
    """这些值里有多大比例"其实可以当数字用"（0 ~ 1）。

    先去掉千分位逗号（半角 `,` 和全角 `，`）和空格再试：销售数据里
    `1,200` 这种写法很常见，直接 `pd.to_numeric` 会得到 0% ——
    那就等于什么都没检测出来。

    注意：这里只是**看出来**它是数字，不会真的改动数据。
    要不要把它变成真正的数字列，是用户看过画像之后自己决定的事。
    """
    if len(non_null) == 0:
        return 0.0

    text = (
        non_null.astype("string")
        .str.strip()
        .str.replace(",", "", regex=False)
        .str.replace("，", "", regex=False)
        .str.replace(" ", "", regex=False)
    )
    return float(pd.to_numeric(text, errors="coerce").notna().mean())


def _try_parse_dates(non_null: pd.Series) -> pd.Series | None:
    """试着把这列文本当日期解析；成功比例够高就返回解析结果，否则 None。

    两道闸门：

    1. `_looks_like_date_text` 先抽样看一眼 —— 值里要同时有数字和日期分隔符
       （`-` `/` `年`），否则直接放弃。没有这道闸门的话，`pd.to_datetime`
       会对每一列普通文本都试着解析一遍：又慢，又会刷一堆
       `UserWarning: Could not infer format`。
    2. 解析成功率 ≥ `DATETIME_SUCCESS_RATIO` 才算日期列。`errors="coerce"`
       会把解析不出来的值变成 `NaT`，所以"成功率"就是非空比例。
    """
    text = non_null.astype("string")
    if not _looks_like_date_text(text):
        return None

    parsed = pd.to_datetime(text, errors="coerce")
    if len(parsed) == 0 or float(parsed.notna().mean()) < DATETIME_SUCCESS_RATIO:
        return None
    return parsed


def _looks_like_date_text(text: pd.Series) -> bool:
    """抽样粗筛：值里既要有数字，又要有日期分隔符，才值得真的去解析。"""
    sample = text.head(DATETIME_SAMPLE_SIZE)
    if len(sample) == 0:
        return False

    has_digit = float(sample.str.contains(r"\d", regex=True).mean())
    has_separator = float(sample.str.contains(r"[-/年月日]", regex=True).mean())
    return has_digit >= NUMERIC_LIKE_RATIO and has_separator >= NUMERIC_LIKE_RATIO


def _top_values(non_null: pd.Series, unique_count: int) -> list[dict[str, Any]]:
    """出现最多的 3 个值（分类 / 文本 / 布尔列用），缺失值不算在内。

    唯一值太多的列直接返回空列表，理由见 `TOP_VALUES_CARDINALITY_LIMIT` 的注释。
    """
    if unique_count > TOP_VALUES_CARDINALITY_LIMIT:
        return []

    counts = non_null.value_counts(dropna=True).head(TOP_VALUES_LIMIT)
    return [{"value": str(value), "count": int(count)} for value, count in counts.items()]


def _to_float(value: Any) -> float | None:
    """把 pandas / numpy 的数字换成纯 Python `float`；NaN 和 inf 一律变成 None。

    为什么必须换：

    1. `np.int64` **不是** `int`，`np.bool_` 也不是 `bool`（`np.float64` 恰好是
       float 的子类）。直接塞进模型，类型迟早会在某个想不到的地方出问题；
    2. JSON 里没有 NaN / Infinity 这两个值。一列只有一个值时 pandas 算出的
       标准差就是 NaN，`json.dumps(..., allow_nan=False)` 会当场抛
       `ValueError: Out of range float values are not JSON compliant: nan`。
       所以"算不出来"必须表达成 `null`（None），而不是 NaN。
    """
    if value is None:
        return None

    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _collect_warnings(
    name: str,
    column: pd.Series,
    semantic_type: str,
    null_ratio: float,
    parsed_dates: pd.Series | None,
    warnings: list[str],
) -> None:
    """把"这列有问题"翻译成人话，攒进 `warnings`。

    写人话的标准：用户看完知道**该做什么**（去重、清洗、删除），
    而不只是知道"有个数字超标了"。

    `parsed_dates` 的语义要认准：它是**我们替用户解析出来的**日期列，只有
    "原本是字符串、被 `_try_parse_dates` 解析成 datetime"这条路才会有值。
    如果是 None，说明这列本来就是 datetime64（没有解析这回事），那列里的空值
    属于缺失值，不是"解析失败" —— 缺失值由上面的 null_ratio 提示负责。
    """
    if null_ratio >= 1.0:
        warnings.append(f"列 {name} 全部为空，建议删除")
    elif null_ratio >= NULL_WARNING_RATIO:
        warnings.append(f"列 {name} 缺失 {null_ratio:.2%}，相关汇总（均值、合计）可能偏低")

    # "看起来是数字、其实是文本"是最容易踩的坑：对这类列求和会直接报错，
    # 或者（更糟）在别的工具里被当成 0 悄悄算错。
    if semantic_type in ("categorical", "text"):
        numeric_ratio = _numeric_like_ratio(column.dropna())
        if numeric_ratio >= NUMERIC_LIKE_RATIO:
            warnings.append(
                f"列 {name} 被识别为文本，因为它包含非数字字符"
                f"（{numeric_ratio:.0%} 的值其实是数字），求和前需要先清洗"
            )

    # 日期列里混着解析不出来的值：告诉用户有几个，别让他以为全都算进去了。
    if semantic_type == "datetime" and parsed_dates is not None:
        failed_count = int(parsed_dates.isna().sum())
        if failed_count > 0:
            warnings.append(f"列 {name} 有 {failed_count} 个值无法解析为日期，统计时按缺失处理")
