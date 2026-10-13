"""app/data/loader.py 的测试。

约定：**真实写文件、真实用 pandas 读**，不 mock。
loader 的价值全在"遇到奇怪的文件会怎样"，而 mock 掉的正好就是那些奇怪的地方 ——
pandas 到底抛哪个异常、空文件长什么样，只有真读一遍才知道。
"""

import openpyxl
import pandas as pd
import pytest

from app import config
from app.data.loader import CSV_ENCODINGS, load_dataframe
from app.errors import AppError
from app.schemas import ErrorCode
from app.schemas_dataset import ALLOWED_EXTENSIONS


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每个测试前后都清一次配置缓存。

    为什么要清：`get_settings()` 带 lru_cache。测试里 monkeypatch 了环境变量之后
    必须让它重新读一次；测试结束后也要清，否则下一个测试会继续看到
    上一个测试留下的临时目录（第一阶段踩过这个坑）。
    """
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _write_xlsx(path, sheets: dict[str, list[list]]) -> None:
    """用 openpyxl 直接写一个多 sheet 的 xlsx。

    为什么不用 pandas 的 to_excel：pandas 会顺手把重名列改名，
    而我们恰恰要测"文件里真的有两个同名列"时会发生什么。
    """
    workbook = openpyxl.Workbook()
    for index, (sheet_name, rows) in enumerate(sheets.items()):
        worksheet = workbook.active if index == 0 else workbook.create_sheet()
        worksheet.title = sheet_name
        for row in rows:
            worksheet.append(row)
    workbook.save(path)


# --------------------------------------------------------------------------
# 正常读取
# --------------------------------------------------------------------------


def test_load_utf8_csv_returns_frame_and_encoding(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("日期,地区,销售额\n2024-01-05,华东,100\n2024-02-06,华北,200\n", encoding="utf-8")

    df, encoding = load_dataframe(path, "csv")

    # 没有 BOM 的普通 UTF-8 也会被第一种编码（utf-8-sig）读成功：
    # 它只是"会顺手去掉 BOM 的 utf-8"，不带 BOM 时照样能读。
    assert encoding == "utf-8-sig"
    assert list(df.columns) == ["日期", "地区", "销售额"]
    assert df.shape == (2, 3)
    assert df["地区"].tolist() == ["华东", "华北"]
    assert df["销售额"].tolist() == [100, 200]


def test_load_gbk_csv_reads_chinese_correctly(tmp_path):
    path = tmp_path / "gbk.csv"
    path.write_text("姓名,城市\n张三,上海\n李四,北京\n", encoding="gbk")

    df, encoding = load_dataframe(path, "csv")

    # utf-8-sig / utf-8 都会解码失败（抛 UnicodeDecodeError），所以落到 gbk。
    assert encoding == "gbk"
    assert df["姓名"].tolist() == ["张三", "李四"]
    assert df["城市"].tolist() == ["上海", "北京"]


@pytest.mark.parametrize("separator", [";", "\t"])
def test_load_other_separators(tmp_path, separator):
    path = tmp_path / "other.csv"
    path.write_text(f"a{separator}b\n1{separator}2\n", encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    assert list(df.columns) == ["a", "b"]
    assert df.shape == (1, 2)


def test_load_does_not_change_cell_values(tmp_path):
    """loader 只做结构清洗，不猜业务含义：带千分位的金额保持原样。"""
    path = tmp_path / "amount.csv"
    path.write_text('商品,单价\nA,"1,200"\nB,"3,400"\n', encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    assert df["单价"].tolist() == ["1,200", "3,400"]


# --------------------------------------------------------------------------
# 列名清洗
# --------------------------------------------------------------------------


def test_duplicate_columns_get_number_suffix(tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("销售额,销售额,其它\n1,2,3\n", encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    # pandas 自己会改成 销售额.1，我们的约定是 销售额_2（见 _make_unique_columns）。
    assert list(df.columns) == ["销售额", "销售额_2", "其它"]


def test_duplicate_suffix_does_not_collide_with_real_column(tmp_path):
    path = tmp_path / "dup2.csv"
    path.write_text("销售额_2,销售额,销售额\n1,2,3\n", encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    # 第二个 销售额 想叫 销售额_2 时发现被占了，于是往后退到 销售额_3。
    assert list(df.columns) == ["销售额_2", "销售额", "销售额_3"]


def test_column_names_are_stripped_and_blank_one_is_named(tmp_path):
    path = tmp_path / "names.csv"
    path.write_text(" 区域 , ,数量\n华东,x,1\n", encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    assert list(df.columns) == ["区域", "未命名列", "数量"]


def test_all_empty_rows_and_columns_are_dropped_and_index_reset(tmp_path):
    path = tmp_path / "empty_parts.csv"
    path.write_text("a,b,c\n1,2,\n3,4,\n,,\n", encoding="utf-8")

    df, _ = load_dataframe(path, "csv")

    assert list(df.columns) == ["a", "b"]  # c 列全空 → 删掉
    assert df.shape == (2, 2)  # 最后一行全空 → 删掉
    assert df.index.tolist() == [0, 1]  # 行号抹平


# --------------------------------------------------------------------------
# 错误路径
# --------------------------------------------------------------------------


def test_header_only_csv_raises_bad_request(tmp_path):
    path = tmp_path / "header_only.csv"
    path.write_text("a,b\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "csv")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST
    assert "只有表头" in excinfo.value.message


def test_empty_csv_raises_bad_request(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "csv")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST
    assert "空" in excinfo.value.message


def test_row_limit_exceeded_raises_bad_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_ROWS", "3")
    config.reset_settings_cache()
    path = tmp_path / "many_rows.csv"
    path.write_text("a\n1\n2\n3\n4\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "csv")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST
    assert "超过了上限 3 行" in excinfo.value.message


def test_column_limit_exceeded_raises_bad_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_COLUMNS", "2")
    config.reset_settings_cache()
    path = tmp_path / "many_columns.csv"
    path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "csv")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST
    assert "超过了上限 2 列" in excinfo.value.message


def test_unknown_ext_raises_unsupported_file(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("a\n1\n", encoding="utf-8")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "txt")

    assert excinfo.value.code is ErrorCode.UNSUPPORTED_FILE
    assert "只支持 csv / xlsx" in excinfo.value.detail


def test_legacy_xls_gets_a_human_hint(tmp_path):
    """旧版 .xls 是用户最常踩的坑，报错要说清下一步该干什么。"""
    path = tmp_path / "old.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0")  # 旧版 Excel 的文件头，内容随便

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "xls")

    assert excinfo.value.code is ErrorCode.UNSUPPORTED_FILE
    assert "另存为 .xlsx" in excinfo.value.detail


@pytest.mark.parametrize("ext", ALLOWED_EXTENSIONS)
def test_every_allowed_extension_is_actually_loadable(tmp_path, ext):
    """白名单里的每个扩展名，loader 都必须真的读得动。

    这条是**防清单漂移**的：`ALLOWED_EXTENSIONS`（路由层拿它做入口白名单）
    和 `load_dataframe` 里那个 `if ext == "csv" / elif ext == "xlsx"` 是两处
    独立的判断。以后谁往白名单里加了扩展名却没在 loader 里实现，
    用户就会遇到"上传通过了，读的时候才说不支持"。
    """
    path = tmp_path / f"probe.{ext}"
    if ext == "csv":
        path.write_text("列\n1\n", encoding="utf-8")
    else:
        _write_xlsx(path, {"Sheet1": [["列"], [1]]})

    df, encoding = load_dataframe(path, ext)

    assert df.shape == (1, 1)
    # 不用 `assert A if cond else B` 这种写法来省两行：分支写在一起时，
    # "这个文件必须命中哪种编码"这层意思会被埋掉，改动时容易连错的东西一起改掉。
    if ext == "xlsx":
        assert encoding is None  # 二进制格式没有编码这回事
    else:
        # ⚠️ 这里**不能**写 `assert encoding in CSV_ENCODINGS`。那是我第一版写错的：
        # `CSV_ENCODINGS` 正是 `_read_csv` 自己挑编码用的那张表，拿它校验自己的
        # 输出属于**循环自证** —— 一个 UTF-8 文件被错误地报成 `gbk` 时，断言照样绿。
        # 实测（把 loader 的返回值强制改成 gbk）：写 `in CSV_ENCODINGS` 绿，
        # 写下面这行红。文件内容是 UTF-8 写的，所以真正该断言的是"命中了 UTF-8 家族"。
        #
        # 用切片而不是手抄一份 ("utf-8","utf-8-sig")：这张表的**语义**是
        # "UTF-8 家族排在最前，后面的 gbk / gb18030 是兜底"。顺序本身就是要保的性质
        # —— UTF-8 文件绝不该落到兜底编码上去。手抄清单则会漂移。
        assert encoding in CSV_ENCODINGS[:2], "UTF-8 写出来的文件必须命中 UTF-8 家族"


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


def test_xlsx_reads_only_the_first_sheet(tmp_path):
    path = tmp_path / "two_sheets.xlsx"
    _write_xlsx(
        path,
        {
            "第一张": [["甲", "乙"], [1, 2], [3, 4]],
            "第二张": [["丙"], [9]],
        },
    )

    df, encoding = load_dataframe(path, "xlsx")

    assert encoding is None  # xlsx 是二进制，没有"编码"这回事
    assert list(df.columns) == ["甲", "乙"]
    assert df.shape == (2, 2)


def test_xlsx_duplicate_headers_are_renamed(tmp_path):
    path = tmp_path / "dup_sheet.xlsx"
    _write_xlsx(path, {"Sheet1": [["销售额", "销售额"], [1, 2], [3, 4]]})

    df, _ = load_dataframe(path, "xlsx")

    assert list(df.columns) == ["销售额", "销售额_2"]


def test_broken_xlsx_raises_bad_request(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is not a real xlsx file")

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "xlsx")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST
    assert "Excel" in excinfo.value.message


def test_empty_xlsx_sheet_raises_bad_request(tmp_path):
    path = tmp_path / "empty_sheet.xlsx"
    _write_xlsx(path, {"Sheet1": []})

    with pytest.raises(AppError) as excinfo:
        load_dataframe(path, "xlsx")

    assert excinfo.value.code is ErrorCode.BAD_REQUEST


def test_xlsx_values_are_read_as_written(tmp_path):
    path = tmp_path / "values.xlsx"
    _write_xlsx(path, {"Sheet1": [["名称", "数量"], ["A", 1], ["B", 2]]})

    df, _ = load_dataframe(path, "xlsx")

    assert df["名称"].tolist() == ["A", "B"]
    assert df["数量"].tolist() == [1, 2]
    assert isinstance(df, pd.DataFrame)
