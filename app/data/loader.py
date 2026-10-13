"""把磁盘上的 CSV / Excel 文件读成一份"干净但没被改过"的 DataFrame。

这个模块负责什么
----------------
1. **选对编码**：CSV 依次尝试 utf-8-sig → utf-8 → gbk → gb18030，谁先成功用谁。
2. **猜对分隔符**：看第一行里 `,` `;` `\\t` 谁出现得最多。
3. **只读 Excel 的第一个 sheet**（`sheet_name=0`），不把整个工作簿读进来。
4. **最小清洗**：列名去首尾空格、重名列改成 `名字_2`、删掉整行/整列为空的行列。
5. **卡上限**：行数超过 `settings.max_rows`、列数超过 `settings.max_columns` 直接报错。

这个模块不负责什么（写清楚，职责才不会越滚越大）
------------------------------------------------
- 不生成 `dataset_id`、不往磁盘写文件、不碰 HTTP —— 那是路由层和 `app/data/store.py` 的事。
- 不判断"这个文件该不该被接收"：扩展名白名单、字节数上限在路由层做（只有它手里有 `UploadFile`）。
- 不改变数据的含义：不改单元格的值、不把 `销售额(元)` 改名叫 `销售额`、
  不把看起来像日期的文本真的转成日期。清洗只处理"列名"和"整行整列为空"这两类问题。
- 不做统计（缺失个数、均值、分位数……那是 `app/data/profiler.py` 的事）。

返回什么
--------
`load_dataframe(path, ext)` 返回 `(df, encoding)`：

- `df` 是清洗后的 DataFrame，交给 store 和 profiler；
- `encoding` 是 CSV **实际读成功**的那种编码（日志里想知道"这个文件是 GBK 的"时用得上）；
  xlsx 是二进制格式，没有"编码"这回事，所以第二项固定是 `None`。
"""

import csv
from pathlib import Path

import openpyxl
import pandas as pd

from app import config
from app.errors import AppError
from app.schemas import ErrorCode

# CSV 编码按这个顺序试。
# 为什么 utf-8-sig 排第一：它是"会顺手去掉 BOM 的 utf-8"。对没有 BOM 的普通
# UTF-8 文件它照样能读，于是"带 BOM"和"不带 BOM"合并成一种情况，少写一次判断。
# 注意它带来一个副作用：绝大多数没有 BOM 的普通 UTF-8 文件也是它读成功的，
# 所以**解码器名字 ≠ 文件真的带 BOM**。对外报的编码要另外看一眼文件头，
# 见 _describe_encoding()。
# 为什么 gbk 排在 gb18030 前面：gb18030 是 gbk 的超集，能读的情况更多，但同一个
# 字节在两者里可能解出不同的字；先让更常见的 gbk 拿主意，它对不上时再交给 gb18030。
#
# 这里**故意不**接 `LookupError`（QA 提过：万一有人把某个名字打错，Python 找不到
# 这个编码会抛 LookupError，用户会看到 500 而不是 400）。不接的理由：打错编码名
# 是我们的 bug，不是用户的文件有问题。把它翻译成"无法识别这个 CSV 的编码"会让
# 用户去反复检查自己那个完全正常的文件 —— 那是拿谎话换一个好看的状态码。
# 500 + 完整 traceback 才是这件事的实话，`app/main.py` 的兜底处理器已经负责它。
CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk", "gb18030")

# UTF-8 的 BOM。用来区分"文件真的带 BOM"和"utf-8-sig 顺手读成功了"。
UTF8_BOM: bytes = b"\xef\xbb\xbf"

# 分隔符先看这几个。谁在第一行里出现得最多就用谁，一个都没出现就按逗号。
CSV_SEPARATORS: tuple[str, ...] = (",", ";", "\t")

# 表头里空着的列名（CSV 里两个连续的逗号、Excel 里的空单元格）统一叫这个。
UNNAMED_COLUMN = "未命名列"


def load_dataframe(path: Path, ext: str) -> tuple[pd.DataFrame, str | None]:
    """读取并做最小清洗，返回 `(DataFrame, 实际命中的 CSV 编码)`。

    参数
    ----
    path : 磁盘上的文件路径。loader 不检查它安不安全 —— 落盘名由路由层按
           `{dataset_id}.{ext}` 生成（见 docs/ARCHITECTURE.md §10）。
    ext  : `"csv"` 或 `"xlsx"`，**由调用方保证**（路由层已经查过扩展名白名单）。
           这里仍然留一个 else 分支，是为了把"调用方传错"变成明确的 415，
           而不是让它走到后面在某个奇怪的地方炸掉。

    返回
    ----
    `(df, encoding)`：encoding **描述这个文件实际是什么编码**
    （utf-8-sig / utf-8 / gbk / gb18030 之一，`utf-8-sig` 只在文件真的带 BOM 时
    才出现，见 `_describe_encoding`）；xlsx 固定是 None。

    抛错
    ----
    空文件 / 只有表头 / 没有列 / 解析失败 / 超过行列上限 → `AppError(BAD_REQUEST)`；
    ext 不是 csv/xlsx → `AppError(UNSUPPORTED_FILE)`（旧版 `.xls` 会额外给一句
    "请在 Excel 里另存为 .xlsx" 的提示，因为那是用户最容易踩的一个坑）。
    """
    settings = config.get_settings()  # 配置一律在函数里现取，见 docs/ARCHITECTURE.md §4.0

    if ext == "csv":
        df, encoding = _read_csv(path)
        # 报出去的编码要描述**文件**，不是描述"哪个解码器读成功了"。
        # 理由见 _describe_encoding()。
        encoding = _describe_encoding(path, encoding)
    elif ext == "xlsx":
        df = _read_excel(path)
        encoding = None
    else:
        # 防御性分支：正常路径上路由层已经用扩展名白名单拒过一次了。
        # 这里再拦一次，是为了让"调用方传错类型"变成一个明确的 415，
        # 而不是走到后面在某个奇怪的地方炸掉。
        # detail 特意为最常见的 .xls 给一句能照着做的话：只说"不支持 xls"，
        # 用户不知道下一步该干什么。
        detail = (
            "暂不支持旧版 .xls 格式，请在 Excel 里另存为 .xlsx 后再上传"
            if ext == "xls"
            else f"只支持 csv / xlsx，收到 ext={ext!r}"
        )
        raise AppError(
            ErrorCode.UNSUPPORTED_FILE,
            f"不支持的文件类型：{ext}",
            detail=detail,
        )

    # 上限检查放在清洗**之前**，理由见 _check_limits 的注释。
    _check_limits(df, settings)

    # 顺序很重要：先判"没有列"，再判"没有数据行"，最后才清洗。
    # 因为一个只有表头的文件（0 行）在"删全空列"那一步会被整列删光 ——
    # 0 行的表里每一列都是全空 —— 最后变成一个 (0, 0) 的空表，
    # 连"你上传的文件只有表头"这句话都说不出来了。
    if len(df.columns) == 0:
        raise AppError(ErrorCode.BAD_REQUEST, "文件里没有解析出任何列")
    if len(df) == 0:
        # detail 特意给一句能对照的：调用方拿到的 detail 如果缺省，会被
        # app/main.py 兜底填成请求路径（"/api/datasets/upload"），
        # 那句对排错没有信息量 —— 看到 file=xxx.csv 才知道是哪个文件。
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "文件里只有表头，没有数据行",
            detail=f"file={path.name}",
        )

    df = _clean(df)

    # 走到这里还是空，说明数据行本身全是空的
    # （Excel 里被格式刷出来的一大片空行就会这样）。
    if len(df) == 0 or len(df.columns) == 0:
        raise AppError(ErrorCode.BAD_REQUEST, "文件里没有有效数据（数据行或数据列全是空的）")

    return df, encoding


def _check_limits(df: pd.DataFrame, settings: config.Settings) -> None:
    """行数 / 列数上限，超了就抛 400。

    为什么在清洗之前查：清洗会删掉全空行。如果先清洗再查，一个
    "20 万行 + 10 万空行"的文件可能刚好混过上限 —— 而它读进内存的那一刻
    就已经把内存撑到了。宁可提前拒绝，也不要先付出代价再判断。
    """
    row_count, column_count = df.shape

    if row_count > settings.max_rows:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"文件有 {row_count} 行，超过了上限 {settings.max_rows} 行",
            detail=f"rows={row_count} max_rows={settings.max_rows}（可用环境变量 MAX_ROWS 调整）",
        )

    if column_count > settings.max_columns:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"文件有 {column_count} 列，超过了上限 {settings.max_columns} 列",
            detail=f"columns={column_count} max_columns={settings.max_columns}（可用环境变量 MAX_COLUMNS 调整）",
        )


def _read_csv(path: Path) -> tuple[pd.DataFrame, str]:
    """按 `CSV_ENCODINGS` 的顺序试编码，第一个成功的就是结果。

    每种编码都走同一套流程：
    读第一行 → 猜分隔符 → 用 csv 模块读真表头 → 列名去重 → 交给 pandas 读全表。

    为什么表头要自己读：见 `_make_unique_columns` 的说明
    （pandas 会把重名列改成 `销售额.1`，不符合我们约定的 `销售额_2`）。
    为什么用 `csv` 模块而不是 `line.split(",")`：表头里可能有
    `"单价,元"` 这种带引号的字段，自己 split 会把它切坏。
    """
    decode_error: UnicodeDecodeError | None = None

    for encoding in CSV_ENCODINGS:
        try:
            separator = _detect_separator(_first_non_empty_line(path, encoding))
            header = _read_header(path, encoding, separator)
            if not header:
                raise AppError(
                    ErrorCode.BAD_REQUEST,
                    "文件是空的，里面没有任何内容",
                    detail=f"file={path.name}",
                )

            df = pd.read_csv(
                path,
                encoding=encoding,
                sep=separator,
                names=_make_unique_columns(header),
                # 第 0 行是表头。上面已经用 csv 模块把它读出来定好列名了，
                # 这里传 header=0 是告诉 pandas"跳过这一行，别拿它当数据"，
                # 然后由 names 覆盖掉 pandas 自己想出来的列名。
                header=0,
            )
            return df, encoding

        except UnicodeDecodeError as exc:
            # 这个编码解不出这个文件，换下一种再试。
            decode_error = exc
        except pd.errors.EmptyDataError as exc:
            # 文件里一个字节都没有：换编码也没用，直接 400。
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "文件是空的，里面没有任何内容",
                detail=str(exc),
            ) from exc
        except pd.errors.ParserError as exc:
            # 某一行的分隔符数量对不上（多一个逗号之类），不是编码问题，换编码也没用。
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "文件内容无法按 CSV 解析（常见原因：某几行的分隔符个数不一致）",
                detail=str(exc),
            ) from exc

    # 四种编码都失败：说明它根本不是文本文件（比如把 xlsx 改名成了 .csv）。
    raise AppError(
        ErrorCode.BAD_REQUEST,
        "无法识别这个 CSV 的编码（已依次尝试 utf-8-sig / utf-8 / gbk / gb18030）",
        detail=str(decode_error),
    ) from decode_error


def _describe_encoding(path: Path, encoding: str) -> str:
    """把"哪个解码器读成功了"翻译成"这个文件实际是什么编码"。

    为什么需要这一步（实测过的真实现象）：
    `CSV_ENCODINGS` 的第一位是 `utf-8-sig`，而 Python 的 `utf-8-sig` 解码器
    对**没有 BOM** 的普通 UTF-8 文件同样能正常解码（BOM 只是可选前缀）。
    于是第一个就命中并返回，永远走不到 `"utf-8"`：
        带 BOM 的文件 → 报 utf-8-sig（对）
        不带 BOM 的普通 UTF-8 文件 → 也报 utf-8-sig（**不对，文件里没有 BOM**）

    后果不是算错数，而是**日志在说一件不真实的事**（路由层会把这句写进
    `[upload] ... 编码 xxx`），排错的人会照着"这是带 BOM 的文件"去查。

    修法刻意选最小的一种：探测顺序与解码逻辑一个字不动（那是核心逻辑，
    动它要重跑整套编码测试），只在读成功之后回头看一眼文件头三个字节。
    这样"怎么读的"和"怎么报的"分开，两边都能单独讲清楚。
    """
    if encoding != "utf-8-sig":
        return encoding
    try:
        with path.open("rb") as handle:
            head = handle.read(len(UTF8_BOM))
    except OSError:
        # 读不了文件头就照原样报，不为一句日志把上传搞失败。
        return encoding
    return "utf-8-sig" if head == UTF8_BOM else "utf-8"


def _first_non_empty_line(path: Path, encoding: str) -> str:
    """只读第一行**非空**内容，用来猜分隔符。

    为什么只读一行：一个几十万行的文件，为了猜一个几乎不会变的分隔符去扫全文，
    是白花的钱。表头这一行足够代表整份文件的分隔符。
    """
    with path.open("r", encoding=encoding, newline="") as handle:
        for line in handle:
            if line.strip():
                return line
    return ""


def _detect_separator(first_line: str) -> str:
    """第一行里 `,` `;` `\\t` 谁出现得最多就用谁；一个都没出现就按逗号。

    平手时按 `CSV_SEPARATORS` 的顺序（逗号优先）：`max()` 在并列时会返回
    先出现的那个，这正好就是我们要的优先级，不用额外写判断。
    """
    counts = {separator: first_line.count(separator) for separator in CSV_SEPARATORS}
    best = max(CSV_SEPARATORS, key=lambda separator: counts[separator])
    return best if counts[best] > 0 else ","


def _read_header(path: Path, encoding: str, separator: str) -> list[str]:
    """读出真正的表头那一行（跳过前面可能存在的空行）。"""
    with path.open("r", encoding=encoding, newline="") as handle:
        for row in csv.reader(handle, delimiter=separator):
            if any(cell.strip() for cell in row):
                return [str(cell) for cell in row]
    return []


def _read_excel(path: Path) -> pd.DataFrame:
    """读 Excel 的**第一个** sheet（`sheet_name=0`）。

    为什么只读第一个：一个工作簿里可能有十几张表，全读进来既慢又占内存，
    而且"用户想分析哪一张"我们并不知道。第一张是约定，写在架构文档里。

    引擎固定 `openpyxl`：它是 .xlsx 的标准选择（老的 .xls 才需要 xlrd）。
    """
    try:
        header = _read_excel_header(path)
        return pd.read_excel(
            path,
            sheet_name=0,
            engine="openpyxl",
            names=_make_unique_columns(header) if header else None,
            header=0,  # 第一行是表头，已经由 _read_excel_header 读过了
        )
    except AppError:
        raise
    except Exception as exc:
        # openpyxl 遇到损坏的文件会抛 BadZipFile / InvalidFileException / KeyError……
        # 类型五花八门，但意思都是同一句话：这个文件不是正常的 xlsx。
        # 所以在这一处统一收成 400，并把原始异常类型写进 detail（排查时有用，
        # 前端只看到上面那句人话）。
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "Excel 文件无法解析（可能已损坏，或者其实不是 xlsx 文件）",
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc


def _read_excel_header(path: Path) -> list[str]:
    """只把第一个 sheet 的第一行读出来，用来定列名。

    为什么要单独读这一行：pandas 也会把重名列改成 `销售额.1`，
    而我们的约定是 `销售额_2`，所以表头必须在读数据之前就定在我们手里。

    `read_only=True` + 只取一行：openpyxl 不会把整张表展开到内存里。
    `data_only=True`：单元格里如果是公式，取公式算出来的值，而不是 `=A1+B1` 这串字
    （与 pandas 内部读 Excel 的取值方式保持一致）。
    """
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        worksheet = workbook.worksheets[0]
        first_row = next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
    finally:
        # 一定要关：Windows 上文件句柄没放开，后面删这个文件会失败。
        workbook.close()
    return ["" if cell is None else str(cell) for cell in first_row]


def _make_unique_columns(columns) -> list[str]:
    """列名去首尾空格 + 重名加 `_2`，返回全新的 `list[str]`，不改传进来的东西。

    规则按顺序执行：

    1. `str(...)` 之后 `strip()`：`" 销售额 "` → `"销售额"`；
    2. 空列名 → `UNNAMED_COLUMN`（`"未命名列"`）；
    3. 撞名 → 加后缀：`销售额`、`销售额_2`、`销售额_3`……
       如果 `销售额_2` 本身也被占了（文件里真有这么一列），就继续往后找。

    为什么要自己改，而不是交给 pandas？
    pandas 遇到重名列会自己改成 `销售额.1` —— 但我们的约定是 `销售额_2`，
    而且 `.1` 这种后缀很容易和文件里真实存在的列名（比如 `单价.1`）撞在一起。
    所以 CSV 和 Excel 都在"读的时候"就把表头定下来（见 `_read_csv` / `_read_excel`）。
    """
    used: set[str] = set()
    result: list[str] = []

    for raw_name in columns:
        base = str(raw_name).strip() or UNNAMED_COLUMN
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        result.append(name)

    return result


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    """最小清洗：只做"不改变数据含义"的三件事。

    1. 列名再去一次空格（pandas 造列名时可能把空格留下来）；
    2. 删掉整行都为空的行、整列都为空的列；
    3. 抹平行号。

    另外说明一个"看着像 bug、其实不是"的现象：**删全空行这件事发生在 pandas 推断 dtype 之后**。
    CSV 末尾只要有一行全空，整数列就会被读成 float64（手工探针里 `订单号` 从 1001 变成 1001.0），
    因为 pandas 在这一列看见了缺失值，而 int 类型存不下缺失值。这是它的推断规则，不是我们读错了；
    要变回整数得先确认"这些值确实都是整数"，那属于用户决策的清洗步骤，这里不擅自转换。

    为什么不顺手把 `"1,200"` 变成 `1200`、把 `"－"` 当成 0？
    因为清洗越界的成本很高：用户看到的会变成另一份数据，而他不知道我们动过手。
    真正的清洗规则应该由用户决定（后面阶段的"数据清洗"步骤），这里只处理"结构上的空"。

    为什么可以直接改传进来的 df：这份 DataFrame 是 `_read_csv` / `_read_excel`
    刚刚造出来的，外面没有别的引用，就地改它不用再复制一整份
    （20 万行复制一次是白花的钱）。
    """
    df.columns = _make_unique_columns(df.columns)

    # 全空行 / 全空列：dropna(how="all") 只删"整行都是缺失值"或"整列都是缺失值"的。
    # CSV 里的空字段、Excel 里的空单元格，读进来都是缺失值，所以这一条对两者都成立。
    # 先删行再删列：行删完可能让某几列变成全空，这一步正好一起收拾掉。
    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")

    # 抹平行号：删掉空行后 index 会出现空洞（0, 1, 3, 7…），
    # 后面写预览、画图、按位置取行都要为此多绕一层，不如现在就归零。
    return df.reset_index(drop=True)
