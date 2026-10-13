r"""生成示例数据集 `data/samples/sales.csv`：一份"有真问题可查"的销售流水。

这个脚本负责什么、不负责什么
----------------------------
负责：
    用固定随机种子造出 12 个月 × 6 个地区 × 30 个商品的销售数据，**故意**掺进
    三种脏数据（缺失值、完全重复行、异常值），再把**磁盘上那个文件**的真实统计
    打印出来。"数据质量检查"要有东西可查，前提是这份文件里真的有东西可查。

不负责：
    不清洗任何东西（脏数据就是留给检查功能查的），不 import `app/` 下的代码，
    也不参与线上请求。它只在你需要重新生成示例文件时跑一次；把这里承诺的性质
    （9 列固定顺序、脏数据真的存在、金额等式成立、两次生成字节一致）钉死的是
    `tests/test_sample_data.py`。

为什么写文件用标准库 `csv` 而不是 `df.to_csv()`
----------------------------------------------
    金额、评分这些字段是我们**自己拼成文本**再写出去的（`f"{profit:.2f}"`），
    浮点怎么写进文件完全由这里说了算。交给 pandas 格式化的话，换一个 pandas
    版本或改了浮点显示选项就可能多一位少一位，文件 md5 跟着变 —— 而"两次跑出
    同一份文件"正是这份数据的验收标准（文件进了 git，不该每次生成都产生 diff）。
    读回来做统计时才用 pandas，因为那是"检查"要做的事。

所有随机都来自固定 seed
-----------------------
    `numpy.random.default_rng(SEED)`，没有 `datetime.now()`、没有时间戳、
    没有随机文件名、不依赖 `set` 迭代序。换行符固定 LF，编码固定 utf-8-sig
    （带 BOM：Excel 双击打开中文不乱码，`app/data/loader.py` 的第一个编码
    就是 utf-8-sig，能正确吃掉 BOM）。

故意造脏（是"数据质量检查"的素材，不是 bug）
------------------------------------------
    1. 缺失值：日期列、销量列、客户评分列各有若干行是空字段。销量为空的行，
       销售额和利润也一起留空 —— 销量没记上却"算出了金额"，是另一种更难解释的脏。
    2. 完全重复行：从已有行里复制若干行插回去，每个字段都一模一样。
    3. 异常值：几笔大单（销量是正常值的几十倍）和几个离谱单价（明显不是真实价）。

怎么跑
------
    & ".\.venv\Scripts\python.exe" scripts\make_sample_data.py
    想写到别处（测试用）：末尾加一个路径参数。
    & ".\.venv\Scripts\python.exe" scripts\make_sample_data.py D:\tmp\other.csv
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 可调参数
SEED = 20241013  # 换掉这个数字，整份文件的数据都会变（测试会立刻红，这是故意的）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "samples" / "sales.csv"

# 9 列，顺序固定：改这里就必须同步改测试与文档里那张表
COLUMNS = ("日期", "地区", "商品", "类别", "销量", "单价", "销售额", "利润", "客户评分")

REGIONS = ("华东", "华北", "华南", "西南", "西北", "东北")
# 各地区体量不同（华东最大、西北最小）：平均分布的话"按地区汇总"就没什么可看的了
REGION_FACTORS = (1.25, 1.00, 1.10, 0.85, 0.70, 0.80)
MONTHS = tuple(range(1, 13))
YEAR = 2024
MONTHS_PER_YEAR = 12
SEASON_AMPLITUDE = 0.25  # 让各月有起伏；这是编的，不是真实行业规律

# 每个类别一个固定毛利率：利润 = 销售额 × 毛利率。数字是编的，但"按类别固定"
# 这件事重要 —— 否则利润和销售额之间没有稳定关系，画图对账时说不清。
GROSS_MARGIN = {"数码": 0.18, "家电": 0.12, "服饰": 0.35, "食品": 0.08, "家居": 0.22}

# 商品目录：(商品名, 类别, 单价（整数元）, 月均基础销量)
# 单价全部取整数元是**刻意的**：这样 销售额 = 销量 × 单价 是整数乘法，
# 手算和工具算必须一模一样，不会出现 0.0000001 的浮点尾巴。
CATALOGUE = (
    ("无线耳机 A1", "数码", 299, 120),
    ("蓝牙音箱 B2", "数码", 399, 80),
    ("智能手表 C3", "数码", 1299, 35),
    ("机械键盘 D4", "数码", 499, 60),
    ("移动电源 E5", "数码", 159, 200),
    ("便携显示器 F6", "数码", 1099, 25),
    ("电饭煲 H1", "家电", 329, 90),
    ("空气炸锅 H2", "家电", 459, 70),
    ("扫地机器人 H3", "家电", 2199, 18),
    ("电风扇 H4", "家电", 189, 110),
    ("加湿器 H5", "家电", 259, 95),
    ("微波炉 H6", "家电", 599, 50),
    ("纯棉T恤 T1", "服饰", 89, 260),
    ("牛仔裤 T2", "服饰", 199, 150),
    ("运动外套 T3", "服饰", 359, 85),
    ("羊毛围巾 T4", "服饰", 129, 120),
    ("帆布鞋 T5", "服饰", 259, 100),
    ("棒球帽 T6", "服饰", 69, 210),
    ("坚果礼盒 X1", "食品", 129, 180),
    ("精品咖啡豆 X2", "食品", 89, 240),
    ("有机大米 X3", "食品", 69, 320),
    ("山茶油 X4", "食品", 159, 130),
    ("手工曲奇 X5", "食品", 49, 380),
    ("蜂蜜 X6", "食品", 79, 200),
    ("记忆枕头 M1", "家居", 199, 140),
    ("床上四件套 M2", "家居", 399, 75),
    ("落地灯 M3", "家居", 299, 60),
    ("收纳箱 M4", "家居", 79, 230),
    ("香薰蜡烛 M5", "家居", 59, 250),
    ("地毯 M6", "家居", 499, 40),
)

# 客户评分：模拟"大多数人打高分、少数人打低分"
SCORE_MEAN = 4.2
SCORE_STD = 0.45
SCORE_MIN = 1.0
SCORE_MAX = 5.0

# ---------------------------------------------------------------- 故意造脏的量
MISSING_DATE_ROWS = 24  # 日期列留空的行数
MISSING_QUANTITY_ROWS = 18  # 销量列留空的行数（金额一起留空）
MISSING_SCORE_ROWS = 27  # 客户评分列留空的行数
DUPLICATE_ROWS = 8  # 额外插入的完全重复行数
BULK_ORDER_POSITIONS = (500, 1200, 1900)  # 大单所在的行号（从 0 数）
BULK_ORDER_MULTIPLIER = 40  # 大单的销量倍数
ABSURD_PRICE_POSITIONS = (700, 1500)  # 离谱单价所在的行号
ABSURD_PRICE = 99999  # 一看就不是真实售价


def _season_factor(month: int) -> float:
    """按月份给一个"淡旺季"系数：只是让折线图有起伏，不是真实规律。"""
    return 1.0 + SEASON_AMPLITUDE * math.sin(2 * math.pi * (month - 3) / MONTHS_PER_YEAR)


def _next_quantity(rng: np.random.Generator, base_quantity: int, month: int, region_index: int) -> int:
    """销量 = 基础销量 × 淡旺季 × 地区体量 × 随机波动，最后取整。"""
    noise = rng.normal(1.0, 0.15)
    value = base_quantity * _season_factor(month) * REGION_FACTORS[region_index] * noise
    return max(1, int(round(value)))  # 销量不可能是 0 或负数


def _next_score(rng: np.random.Generator) -> float:
    """客户评分：正态分布截断到 1.0~5.0，保留一位小数。"""
    value = min(SCORE_MAX, max(SCORE_MIN, rng.normal(SCORE_MEAN, SCORE_STD)))
    return round(value, 1)


def _build_row(
    month: int,
    day: int,
    region: str,
    product: str,
    category: str,
    quantity: int,
    price: int,
    score: float,
) -> list[str]:
    """拼一行 9 个字段（全是字符串，因为 CSV 里本来就只有文本）。"""
    sales = quantity * price  # 整数乘法：精确
    profit = round(sales * GROSS_MARGIN[category], 2)
    return [
        f"{YEAR}-{month:02d}-{day:02d}",  # 写成字符串：日期怎么解析交给读取方
        region,
        product,
        category,
        str(quantity),
        str(price),
        str(sales),
        f"{profit:.2f}",  # 固定两位小数：浮点怎么写进文件由我们说了算
        f"{score:.1f}",  # 评分固定一位小数
    ]


def _rebuild_amounts(row: list[str]) -> None:
    """按当前 销量/单价 重算 销售额/利润。

    就地改（不返回新行）：这些行是刚造的，列表外面没有任何引用，
    复制一遍整表纯属浪费。
    """
    sales = int(row[4]) * int(row[5])
    row[6] = str(sales)
    row[7] = f"{round(sales * GROSS_MARGIN[row[3]], 2):.2f}"


def _apply_outliers(rows: list[list[str]]) -> None:
    """造异常值：几笔大单 + 几个离谱单价（金额跟着重算，不然等式立刻就不成立了）。"""
    for position in BULK_ORDER_POSITIONS:
        if position < len(rows):
            rows[position][4] = str(int(rows[position][4]) * BULK_ORDER_MULTIPLIER)
            _rebuild_amounts(rows[position])
    for position in ABSURD_PRICE_POSITIONS:
        if position < len(rows):
            rows[position][5] = str(ABSURD_PRICE)
            _rebuild_amounts(rows[position])


def _apply_missing_values(rng: np.random.Generator, rows: list[list[str]]) -> None:
    """造缺失值：日期 / 销量 / 客户评分各挑若干行写成空字符串。

    `rng.choice(..., replace=False)` 保证同一列里不会重复挑到同一行；
    两列挑到同一行是允许的（真实数据里"日期和评分同时没填"很常见）。
    """
    plans = (
        (MISSING_DATE_ROWS, 0, False),
        (MISSING_QUANTITY_ROWS, 4, True),  # 销量没填，销售额和利润也没法算
        (MISSING_SCORE_ROWS, 8, False),
    )
    for count, column_index, blank_amounts in plans:
        for position in rng.choice(len(rows), size=count, replace=False):
            row = rows[int(position)]
            row[column_index] = ""
            if blank_amounts:
                row[6] = ""
                row[7] = ""


def _insert_duplicates(rng: np.random.Generator, rows: list[list[str]]) -> None:
    """插入完全重复行：复制已有行再插回去。"""
    sources = rng.integers(0, len(rows), size=DUPLICATE_ROWS).tolist()
    positions = rng.integers(0, len(rows), size=DUPLICATE_ROWS).tolist()
    # 按插入位置从前往后插，结果才和"一个个插"的直觉一致
    for source, position in sorted(zip(sources, positions), key=lambda pair: pair[1]):
        # 必须 list(...) 复制一份：直接 insert 同一个列表对象，两行会共享内存，
        # 以后改一处会连带改另一处
        rows.insert(int(position), list(rows[int(source)]))


def build_rows() -> list[list[str]]:
    """造出全部数据行（不含表头）。同一个 SEED 必然得到同一份结果。"""
    rng = np.random.default_rng(SEED)
    rows: list[list[str]] = []
    for month in MONTHS:
        for region_index, region in enumerate(REGIONS):
            for product, category, price, base_quantity in CATALOGUE:
                day = int(rng.integers(1, 29))  # 1~28 号：保证每个月都有数据
                quantity = _next_quantity(rng, base_quantity, month, region_index)
                score = _next_score(rng)
                rows.append(
                    _build_row(month, day, region, product, category, quantity, price, score)
                )
    _apply_outliers(rows)
    _apply_missing_values(rng, rows)
    _insert_duplicates(rng, rows)
    return rows


def write_sample(path: Path) -> int:
    """把示例数据写到 path，返回数据行数（不含表头）。"""
    rows = build_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        # newline="" 关掉 Python 的换行翻译，lineterminator="\n" 明确用 LF。
        # 用默认的 \r\n 会让 Windows 和 Linux 生成的文件字节不同，md5 就对不上了。
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return len(rows)


def describe(path: Path) -> pd.DataFrame:
    """用 pandas 把**磁盘上的文件**读回来，打印它的真实统计。

    必须是"读回来"而不是"打印我打算生成什么"：只有读回来，脚本打印的数字才能
    和另一个人用 pandas 读同一个文件算出来的数字对上 —— 这正是验收要看的东西。
    用 encoding="utf-8-sig" 是为了让 pandas 明确吃掉 BOM，否则第一列列名会带上
    一个看不见的 \\ufeff。
    """
    frame = pd.read_csv(path, encoding="utf-8-sig")

    print(f"文件: {path}")
    print(f"行列数: {frame.shape[0]} 行 × {frame.shape[1]} 列")
    print("每列缺失数:")
    for column in frame.columns:
        print(f"  {column}: {int(frame[column].isna().sum())}")
    print(f"完全重复行数: {int(frame.duplicated().sum())}")
    print("各列 dtype:")
    for column, dtype in frame.dtypes.items():
        print(f"  {column}: {dtype}")
    dates = pd.to_datetime(frame["日期"], errors="coerce")
    print(f"日期跨度: {dates.min():%Y-%m-%d} ~ {dates.max():%Y-%m-%d}（{dates.dt.month.nunique()} 个月）")
    print(
        f"地区数: {frame['地区'].nunique()}，"
        f"商品数: {frame['商品'].nunique()}，"
        f"类别数: {frame['类别'].nunique()}"
    )
    print(
        f"销售额: 最大 {frame['销售额'].max():.0f}，"
        f"中位数 {frame['销售额'].median():.0f}，"
        f"单价最大 {frame['单价'].max():.0f}"
    )
    return frame


def main(argv: list[str]) -> int:
    """命令行入口：写文件 → 读回磁盘上那个文件 → 打印统计。"""
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    row_count = write_sample(path)
    print(f"已写入 {row_count} 行数据（另有 1 行表头）")
    describe(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
