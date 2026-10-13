"""把 `scripts/make_sample_data.py` 对示例数据集的承诺钉住。

测的是"磁盘上那个文件真的有什么"，不是"脚本大概会生成什么"：所有断言都先把
`data/samples/sales.csv` 读进来，所以文件被人手改坏了、或者生成逻辑漂移了，
这里都会红。断言只写下限与不变式，不写死"一共 2168 行"这种会腐烂的数字
（商品目录一扩、行数就变，但"每个月每个地区每个商品至少一行"永远成立）。

最后两个用例是"可重复生成"的看门人：重新生成的文件必须与仓库里那份**逐字节
相同**，否则文件进 git 之后每次生成都会产生 diff，示例数据就成了不可复现的东西。
"""

from __future__ import annotations

import codecs
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from scripts.make_sample_data import (
    COLUMNS,
    DEFAULT_OUTPUT,
    GROSS_MARGIN,
    PROJECT_ROOT,
    REGIONS,
    write_sample,
)

SAMPLE_PATH = DEFAULT_OUTPUT
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "make_sample_data.py"

MIN_MONTHS = 12
MIN_REGIONS = 6
MIN_PRODUCTS = 30
MIN_CATEGORIES = 5
MIN_ROWS = MIN_MONTHS * MIN_REGIONS * MIN_PRODUCTS  # 每个月 × 地区 × 商品至少一行


@pytest.fixture(scope="module")
def sample() -> pd.DataFrame:
    """整份示例数据（读一次，多个用例共用）。"""
    return pd.read_csv(SAMPLE_PATH, encoding="utf-8-sig")


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 文件与列


def test_sample_file_is_where_the_architecture_doc_says_it_is() -> None:
    """文件名口径以架构文档 §2 为准：data/samples/sales.csv。"""
    assert SAMPLE_PATH == PROJECT_ROOT / "data" / "samples" / "sales.csv"
    assert SAMPLE_PATH.is_file()
    assert SAMPLE_PATH.stat().st_size > 0


def test_columns_are_exactly_the_nine_names_in_order(sample: pd.DataFrame) -> None:
    assert len(COLUMNS) == 9
    assert list(sample.columns) == list(COLUMNS)


# ---------------------------------------------------------------- 规模下限


def test_size_and_variety_reach_the_designed_lower_bounds(sample: pd.DataFrame) -> None:
    assert len(sample) >= MIN_ROWS
    assert set(sample["地区"].dropna().unique()) == set(REGIONS)
    assert sample["商品"].nunique() >= MIN_PRODUCTS
    assert sample["类别"].nunique() >= MIN_CATEGORIES
    months = pd.to_datetime(sample["日期"], errors="coerce").dt.to_period("M").nunique()
    assert months >= MIN_MONTHS


# ---------------------------------------------------------------- 故意造脏真的存在


def test_dirty_missing_values_really_exist(sample: pd.DataFrame) -> None:
    """缺失值不是"文档里说造了"，而是这三列里真的有空的。"""
    for column in ("日期", "销量", "客户评分"):
        assert int(sample[column].isna().sum()) > 0, f"{column} 列应当有故意造的缺失值"

    # 销量没记上的行，销售额和利润也一起留空：否则会留下"没有销量却有金额"的行
    missing_quantity = sample[sample["销量"].isna()]
    assert missing_quantity["销售额"].isna().all()
    assert missing_quantity["利润"].isna().all()


def test_dirty_duplicate_rows_really_exist(sample: pd.DataFrame) -> None:
    assert int(sample.duplicated().sum()) > 0


def test_dirty_outliers_really_exist(sample: pd.DataFrame) -> None:
    """大单与离谱单价都要真的在文件里，而且和正常量级拉开数量级。"""
    sales = sample["销售额"].dropna()
    assert sales.max() >= 10 * sales.median()

    price = sample["单价"].dropna()
    assert price.max() >= 10 * price.median()


# ---------------------------------------------------------------- 金额不变式


def test_sales_equals_quantity_times_price_on_every_complete_row(sample: pd.DataFrame) -> None:
    """销售额 = 销量 × 单价 必须对每一行精确成立（整数乘法，不允许浮点误差）。"""
    complete = sample.dropna(subset=["销量", "单价", "销售额"])
    assert len(complete) > 0
    assert (complete["销售额"] == complete["销量"] * complete["单价"]).all()


def test_profit_is_sales_times_the_category_margin(sample: pd.DataFrame) -> None:
    complete = sample.dropna(subset=["销售额", "利润"])
    assert len(complete) > 0
    expected = (complete["销售额"] * complete["类别"].map(GROSS_MARGIN)).round(2)
    assert (complete["利润"] - expected).abs().max() < 1e-9
    # 利润保留两位小数，不会出现 7857.719999999999
    assert (complete["利润"].round(2) == complete["利润"]).all()


def test_score_is_between_one_and_five_with_one_decimal(sample: pd.DataFrame) -> None:
    scores = sample["客户评分"].dropna()
    assert len(scores) > 0
    assert scores.between(1.0, 5.0).all()
    assert (scores * 10 == (scores * 10).round()).all()


# ---------------------------------------------------------------- 可重复生成


def test_file_is_utf8_with_bom_and_lf_only() -> None:
    """这两个字节级性质是 md5 稳定的前提，也是 Excel 不乱码的前提。"""
    raw = SAMPLE_PATH.read_bytes()
    assert raw.startswith(codecs.BOM_UTF8), "带 BOM 才能让 Excel 双击打开不乱码"
    assert b"\r" not in raw, "只用 LF：Windows 与 Linux 生成的文件才会逐字节一致"
    first_line = raw.decode("utf-8-sig").splitlines()[0]
    assert first_line.split(",") == list(COLUMNS)


def test_regenerating_in_process_reproduces_the_committed_file(tmp_path: Path, sample: pd.DataFrame) -> None:
    again = tmp_path / "again.csv"

    row_count = write_sample(again)

    assert row_count == len(sample)
    assert again.read_bytes() == SAMPLE_PATH.read_bytes()


def test_running_the_script_reproduces_the_committed_file(tmp_path: Path) -> None:
    """用子进程跑真实命令行入口（同时顺带验证"脚本能被直接跑"这件事）。"""
    again = tmp_path / "from_subprocess.csv"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 不设的话中文统计可能乱码

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(again)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "行列数" in result.stdout and "完全重复行数" in result.stdout
    assert _md5(again) == _md5(SAMPLE_PATH)
