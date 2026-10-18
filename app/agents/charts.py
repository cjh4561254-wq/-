"""图表生成：把**已经算好的**工具结果画成一张 PNG。

## 为什么它只吃 `ToolResult`，不吃 `DataFrame`

架构文档 §3.4 的两条"不能做"合起来就是这一句话：② 不读原始 `df`；
③ 绝不接受模型传来的原始数据点。理由是"图上的数字必须和报告里的数字
来自同一个地方"：如果画图这一步自己去 `df` 里重新 `groupby` 求和，就会出现
"报告说 2487 万、图上柱子是 2500 万"这种最难查的 bug —— 两个数字都对，
只是算的不是同一批行（一个去了重、一个没去重）。所以输入只有
`tool_result.data`（工具已经算好的 `rows` / `pairs`），
画图这一步只做"把已有的数字摆到坐标轴上"。

## 为什么 `x_field` / `y_field` 允许兜底

`ChartRequest` 里的字段名是 planner 写的（模型规划那条路上，它是模型写的），
所以一定会出现"写错一个字"的情况。这里的原则是**能画就画，画不了才报错**：
请求的字段不在结果里时，退回工具**实际返回的列名**并落一条 warning。
反过来，如果这里硬取 `row[chart.x_field]`，用户看到的是"出图失败"，
真正的原因却是"字段名写错了一个字" —— 这条错误信息对排查没有任何帮助。

## 三条技术铁律（少一条都会出问题）

1. **`matplotlib.use("Agg")`**：服务器和 CI 都没有显示器，用默认后端会直接崩。
   它必须在 `import matplotlib.pyplot` **之前**调用，所以本文件的 import 顺序
   是刻意排的，不要"整理"成字母序。
2. **中文字体必须显式设置**：matplotlib 自带字体没有汉字，标题会变成一排方框。
   而这台机器上"方框"不会报错、不会让任何测试变红，只有人眼看截图才发现 ——
   所以本文件在 import 时就把字体装好（见 `_install_chinese_font`）。
3. **`MPLCONFIGDIR` 要在 import matplotlib 之前设好**：否则 matplotlib 会去写
   用户目录下的缓存（本机实测会被权限拒绝并报 `mkdir -p failed`）。
   `app/config.py:31-33` 在 import 时 `setdefault` 了这个环境变量，
   所以下面**先** `from app import config`，**再** import matplotlib。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app import config  # 必须排在 matplotlib 之前：它在 import 时设好 MPLCONFIGDIR
from app.schemas_tool import ChartResult, ToolResult
from app.schemas_workflow import ChartRequest

import matplotlib

# 无显示环境专用后端。写在 import pyplot 之前，见模块 docstring 铁律 1。
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  （顺序是刻意的）
from matplotlib import font_manager  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

logger = logging.getLogger(__name__)

# 出图分辨率。120 是"网页里够清晰、文件又不至于几 MB"的折中：
# 默认的 100 在高分屏上偏糊，200 会让一张 20 根柱子的图超过 500 KB。
_DPI = 120

# 柱子/线条的颜色。固定一个颜色而不是让 matplotlib 每次循环换色：
# 同一份数据两次生成的图看起来必须一样，否则"报告截图"和"网页上的图"会对不上。
_BAR_COLOR = "#3b6ea5"
_LINE_COLOR = "#c0504d"

# X 轴标签超过这个数量就斜着写，否则中文标签会互相压在一起变成一团黑。
_LABEL_ROTATE_THRESHOLD = 8

# --------------------------------------------------------------------------
# 企业级出图的视觉常量（task-7）
#
# 这些值不是"审美偏好"，每一条都对应上面那张图在真实使用里出过或会出的问题：
# 没有网格线时 20 根柱子的高度只能靠肉眼比；坐标轴用科学计数法（1e7）时
# 用户必须自己数零；标签一多就互相压住 —— 所以下面是"可读性配置"，不是装饰。
# --------------------------------------------------------------------------

# 画布尺寸（英寸）。8×4.8 @120dpi = 960×576：
# 宽高比 5:3，贴进报告页（正文 measure 78ch）和 1440px 工作台的图表卡都不用缩放。
# 改这里会同时改落盘像素数，`tests/test_charts_coverage.py` 用的是
# "实测英寸 × 声明 dpi"的关系断言，所以改数值本身不会让它变红 —— 但改动要有理由。
_FIGURE_SIZE = (8.0, 4.8)

# 画布与坐标区都是白底：图表会被贴进报告页（白纸）和深色工作台两种背景里，
# 透明或深色底会在其中一边难看。白底是唯一两边都不出错的取法。
_SURFACE = "#ffffff"
# 网格线：只画横向（比较的是高度，不是宽度），颜色足够淡才不抢柱子。
_GRID_COLOR = "#e6e9ee"
# 轴线：比网格重一档，让"坐标框"这件事看得见但不刺眼。
_SPINE_COLOR = "#c8ced8"
# 刻度与标签文字。
_TICK_COLOR = "#5a6470"
# 图表下方的口径脚注（说明单位、点被丢弃等）。
_NOTE_COLOR = "#6b7480"

_TITLE_SIZE = 13.0  # 标题：与正文 14.5px 同档，保证"这是图的名字"
_TICK_SIZE = 10.0  # 刻度标签：再小就在 1080p 上读不动了
_LABEL_SIZE = 9.5  # 轴名与数值直标：辅助信息，最小一档
_NOTE_SIZE = 8.5  # 脚注：全图最小，只给"需要时才看"的信息

# 数值直标的数量上限。超过就只标头尾：
# 24 根柱子上每根都写数字，数字本身会变成噪声（互相压住之后谁读不清）。
_MAX_VALUE_LABELS = 12

# 折线图"每点都标数值"的上限。**比柱状图小得多，而且必须小**：
# 折线的点是挤在一条横线上的，12 个月的两点间距只有 60 多像素，
# 真实数据里 `5,135,498` 与 `5,319,825` 正好是相邻两点 —— 实拍时两个数字直接叠住。
# 超过这个数量就只标首/尾/最高/最低四个（它们分布在四个不同位置，不会互相压）。
_LINE_LABEL_ALL_LIMIT = 3

# 单位换算阈值。用中文数量级而不是科学计数法：
# `1e7` 要求读者自己数零，`2,487 万` 不用。
_UNIT_TEN_THOUSAND = 10_000.0
_UNIT_HUNDRED_MILLION = 100_000_000.0

# 轴上超过这个数量级的数字改用"万/亿"记（否则 8 位数字会把左侧留白吃光）。
_AXIS_UNIT_THRESHOLD = 1_000_000.0

# 饼图最多画几块。超过就把小的合并成"其他"：
# 18 个 1.5% 的扇形在 960px 宽的图上谁也分不出边界，图会从"说明占比"退化成"彩色圆盘"。
_MAX_PIE_SLICES = 6
_PIE_OTHER_LABEL = "其他"

# 饼图色板（只有饼图用它）。第一块与 `_BAR_COLOR` 同色，所以"单分类饼图"和
# 柱状图看起来是同一个产品出的图。**顺序固定**是有意的：同一份数据两次出图
# 必须完全一样，否则"报告里的图"和"网页上的图"会被当成两张不同的图。
#
# 这几个值不是挑好看，是**算出来的**（`.tmp/charts/check_palette.py` 穷举 21 个
# 候选色的组合，输出见 `.tmp/charts/palette_report.txt`）：
#
# - 对白底全部 ≥ 3:1（WCAG 2.1 对图形元素的阈值，实测 3.47:1 ～ 7.99:1）；
# - 在 protanopia / deuteranopia（Machado 2009，严重度 1.0）模拟下，
#   15 对里**最小** ΔE*ab = 19.6（判据 15）—— 初版沿用 tokens.css 的
#   `--ds-chart-1..6`，实测最小只有 4.2（`#3b6ea5` ↔ `#5b5fa8` 在
#   deuteranopia 下几乎同一个颜色），所以这里换掉了。
# - 两个蓝（`#1f4e9c` / `#4a7fc0`）刻意不相邻：色盲下它们是最接近的一对，
#   中间夹一个暖色，饼图上就不会出现"两块相邻的蓝"。
#
# 比色板更重要的那条纪律：**饼图每一块都带文字标签**，信息不靠颜色单独承载。
_PIE_PALETTE = (
    "#1f4e9c",  # 深蓝（与品牌主色同族，作为第 1 块）
    "#a8621f",  # 赭石（暖）
    "#9c8f5a",  # 橄榄
    "#4a7fc0",  # 亮蓝（与第 1 块是最接近的一对，故与深蓝隔开）
    "#7c7f96",  # 蓝灰
    "#6f6a5f",  # 暖灰
)

# 中文字体候选。按顺序找第一个存在的：微软雅黑是 Windows 标配，
# 黑体（SimHei）是它的老备份，DejaVu Sans 放最后只是为了"一个都没有时不要崩"。
_CJK_FONT_FILES = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
)
_CJK_FONT_NAMES = ("Microsoft YaHei", "SimHei", "DejaVu Sans")


def _install_chinese_font() -> None:
    """把中文字体注册进 matplotlib，并让它成为默认字体。

    两件事都要做，缺一不可：

    - `addfont` 是把字体**文件**告诉 matplotlib。这台机器上 `msyh.ttc` 本来
      就在字体表里，所以"新增字体名"是空列表 —— 这一步在别的机器上才有用
      （比如精简版 Windows 没扫描到 `C:\\Windows\\Fonts`）。
    - 设置 `rcParams["font.sans-serif"]` 才是真正生效的那一步：matplotlib
      默认的字体列表里没有汉字，标题会渲染成方框。

    `axes.unicode_minus = False` 是配套的：微软雅黑里没有 Unicode 减号（U+2212），
    负数坐标轴会变成方框，改用 ASCII 的 `-` 就正常了。
    """
    for path in _CJK_FONT_FILES:
        if not path.is_file():
            continue
        try:
            font_manager.fontManager.addfont(str(path))
        except Exception:  # 字体文件损坏 / 格式不认识，都不该让整个服务起不来
            logger.warning("中文字体 %s 注册失败，改用下一个候选", path)

    plt.rcParams["font.sans-serif"] = list(_CJK_FONT_NAMES)
    plt.rcParams["axes.unicode_minus"] = False


_install_chinese_font()


@dataclass(frozen=True)
class _Points:
    """从工具结果里抠出来的"可以画的东西"。

    为什么用一个 dataclass 而不是返回一个 5 元组：这里有一半字段是"实际用到的
    字段名"（要和 `ChartResult` 对得上），散着传很容易把顺序搞反 ——
    而顺序搞反的后果是"图上画的是另一列"，看起来一切正常。
    """

    x_field: str  # 实际用在 X 轴上的字段名
    y_field: str  # 实际用在 Y 轴上的字段名
    labels: list[str]  # X 轴的刻度标签（柱状/折线/饼图用）
    values: list[float]  # Y 轴的数值
    x_values: list[float]  # 散点图的数值 X 轴（其它图型不用）
    use_row_order: bool  # True 表示 X 轴是行号（top_n 只有一列，没有第二个字段）


def render_chart(tool_result: ToolResult, chart: ChartRequest, workflow_id: str) -> ChartResult:
    """把一次工具调用的结果画成 PNG，返回描述这张图的 `ChartResult`。

    参数：
        tool_result: 工具的执行结果。**只读它的 `data` / `columns` / `task_id`**。
        chart:       planner 配的图型与字段（见 `app/schemas_workflow.py:102`）。
        workflow_id: 32 位 hex 的工作流编号，决定 PNG 落在哪个子目录。

    返回值里的 10 个字段一个都不能少：接口 6、接口 8 和前端图表区共用这一份定义。

    失败时**抛异常**（不返回 None）：调用方要区分"这次没有图"和"出图失败了"，
    前者是正常的（任务没配图），后者要在校验里记一条 `MISSING_CHART_FILE`。
    返回 None 会让这两种情况长得一模一样。
    """
    points = _extract_points(tool_result, chart)
    chart_id = uuid4().hex[:12]

    # 目录按 workflow_id 分子目录：同一个工作流的所有图在一起，删的时候不会误删别人的。
    target_dir = config.get_settings().charts_path / workflow_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{chart_id}.png"

    figure: Figure | None = None
    try:
        figure = _draw(points, chart)
        figure.savefig(target, dpi=_DPI)
    except Exception:
        # 半张图（写到一半失败）不能留在磁盘上：接口 8 是按文件名直接返回文件的，
        # 留下一个 0 字节的 PNG，前端会显示"裂图"而不是"这次没有图"。
        target.unlink(missing_ok=True)
        raise
    finally:
        if figure is not None:
            plt.close(figure)  # 不关的话，一次分析出 8 张图就会攒 8 个画布在内存里

    return ChartResult(
        chart_id=chart_id,
        filename=f"{chart_id}.png",
        title=chart.title,
        chart_type=chart.chart_type,
        url=f"/api/workflows/{workflow_id}/charts/{chart_id}.png",
        source_task_id=tool_result.task_id,
        x_field=points.x_field,
        y_field=points.y_field,
        point_count=len(points.values),
        created_at=datetime.now(timezone.utc),
    )


def _extract_points(tool_result: ToolResult, chart: ChartRequest) -> _Points:
    """从 `tool_result.data` 里抠出"标签 + 数值"两组列表。

    取数顺序：先看 `rows`（分组汇总 / 排行 / 时间序列 / 分组比较都是这个形状），
    再看 `pairs`（相关系数只有"哪两列 + 一个 r"，没有原始数据点）。
    两个都没有就没法画 —— 直接抛，让调用方去记 `MISSING_CHART_FILE`。
    """
    rows, columns = _rows_and_columns(tool_result)
    if not rows:
        raise ValueError(
            f"工具 {tool_result.tool_name} 的结果里没有可以画图的数据行（data 里既没有 rows 也没有 pairs）"
        )

    x_field = _resolve_field(chart.x_field, rows, columns, index=0)
    y_field = _resolve_field(chart.y_field, rows, columns, index=-1)

    # `top_n` 只返回一列（排序列本身），没有第二个字段可以当 X 轴 ——
    # 这时用行号当标签，图读起来就是"第 1 名到第 N 名"。
    use_row_order = x_field == y_field

    labels: list[str] = []
    values: list[float] = []
    x_values: list[float] = []
    for index, row in enumerate(rows):
        value = _as_number(row.get(y_field))
        if value is None:
            # 这一行画不出来（空值 / 文本）。跳过而不是当 0：把缺失画成 0
            # 会让"没有数据"看起来像"数值是零"，那是两件完全不同的事。
            continue
        labels.append(str(index + 1) if use_row_order else str(row.get(x_field)))
        values.append(value)
        x_values.append(_as_number(row.get(x_field)) or 0.0)

    if not values:
        raise ValueError(f"工具 {tool_result.tool_name} 的 {y_field} 列里没有可以画的数值")

    if chart.chart_type == "pie":
        kept = [(label, value, x) for label, value, x in zip(labels, values, x_values) if value > 0]
        if len(kept) < len(values):
            # 饼图里没有"负数扇形"这种东西，硬画 matplotlib 会直接抛。
            # 丢掉并落一条 warning：用户看图时至少知道少了几块，而不是拿到一张废图。
            logger.warning(
                "饼图丢掉 %d 个非正数值（饼图无法表示负数或零），实际画上去 %d 块",
                len(values) - len(kept),
                len(kept),
            )
        if not kept:
            raise ValueError("饼图需要至少一个正数，当前结果里一个都没有")
        labels = [item[0] for item in kept]
        values = [item[1] for item in kept]
        x_values = [item[2] for item in kept]

    if chart.chart_type == "scatter":
        numeric = [(x, value) for x, value in zip(x_values, values) if _as_number(x) is not None]
        if len(numeric) < 2:
            raise ValueError("散点图需要两个数值列，当前结果里凑不出两个数值点")
        x_values = [item[0] for item in numeric]
        values = [item[1] for item in numeric]
        labels = labels[: len(numeric)]

    return _Points(
        x_field=x_field,
        y_field=y_field,
        labels=labels,
        values=values,
        x_values=x_values,
        use_row_order=use_row_order,
    )


def _rows_and_columns(tool_result: ToolResult) -> tuple[list[dict[str, Any]], list[str]]:
    """把工具结果里的"行"和"列名"取出来，兼容 `rows` 与 `pairs` 两种形状。"""
    data = tool_result.data or {}

    rows = data.get("rows")
    if isinstance(rows, list) and rows:
        dict_rows = [row for row in rows if isinstance(row, dict)]
        if dict_rows:
            columns = [str(name) for name in (tool_result.columns or [])]
            return dict_rows, columns or list(dict_rows[0].keys())

    pairs = data.get("pairs")
    if isinstance(pairs, list) and pairs:
        # 相关系数的 `pairs` 是 `{"x": 列名, "y": 列名, "r": 系数}`，没有原始数据点，
        # 所以画成"每一对列一根柱子"的柱状图，而不是散点图。
        pair_rows = [
            {"列对": f"{item.get('x')}-{item.get('y')}", "相关系数": item.get("r")}
            for item in pairs
            if isinstance(item, dict)
        ]
        if pair_rows:
            return pair_rows, ["列对", "相关系数"]

    return [], []


def _resolve_field(name: str, rows: list[dict[str, Any]], columns: list[str], index: int) -> str:
    """把 planner 请求的字段名落到"结果里真的有的那个键"上。

    三级兜底：请求的字段 → 工具自己报告的列名（`tool_result.columns`）→ 行里第一个/最后一个键。
    每一级都落一条 warning，因为**每一次兜底都意味着 planner 和工具对不上**，
    这种不一致应该被看见，而不是被静默修好。
    """
    keys = list(rows[0].keys())
    if name in keys:
        return name

    if columns:
        candidate = columns[index]
        if candidate in keys:
            logger.warning("图表请求的字段 %s 不在工具结果里，改用 %s", name, candidate)
            return candidate

    fallback = keys[index]
    logger.warning("图表请求的字段 %s 与工具报告的列名都不在结果里，改用 %s", name, fallback)
    return fallback


def _draw(points: _Points, chart: ChartRequest) -> Figure:
    """按 `chart_type` 画图。只做"摆位置"，不做任何聚合。

    task-7 之后这里多了一层"可读性与防误导"的规则，每条都在注释里写了理由：
    柱状图**必须包含 0 基线**（否则柱高比例会被视觉放大，是最常见的误导性图表）、
    轴刻度用"万/亿"而不是科学计数法、标签过多时只标头尾、饼图分类过多时合并为
    "其他"、图表下方给一行口径脚注（点数、单位、被合并的项）。这些都不改数字，
    只改"数字摆在哪里、怎么写"。
    """
    figure, axes = plt.subplots(figsize=_FIGURE_SIZE, facecolor=_SURFACE)
    axes.set_facecolor(_SURFACE)
    axes.set_title(chart.title, fontsize=_TITLE_SIZE, color=_TICK_COLOR, pad=12, loc="left")

    notes: list[str] = []

    if chart.chart_type == "bar":
        _draw_bar(axes, points)
        notes.append(f"柱高从 0 起算（不截断纵轴，避免把差异画大）· 共 {len(points.values)} 个分组")
    elif chart.chart_type == "line":
        _draw_line(axes, points)
        notes.append(f"共 {len(points.values)} 个数据点 · 按给定顺序等距排列（不是按时间比例）")
    elif chart.chart_type == "pie":
        merged = _draw_pie(axes, points)
        if merged:
            notes.append(f"分类过多，最小的 {merged} 项已合并为「{_PIE_OTHER_LABEL}」")
    else:  # scatter —— ChartType 只有四个取值，走到这里只可能是散点
        axes.scatter(points.x_values, points.values, color=_BAR_COLOR, s=34, alpha=0.85, zorder=3)
        notes.append(f"共 {len(points.values)} 个数据点 · 每个点是一行真实记录")

    if chart.chart_type != "pie":
        _style_cartesian(axes, points, chart)
        if chart.chart_type == "bar":
            notes.append("轴与图的数值以报告正文表格为准")

    # 口径脚注：贴在图的左下角、字体最小。它的作用是让"这张图少画了几个点"
    # 变成一件用户看得见的事 —— 不说话地丢掉数据，比丢数据更糟。
    if notes:
        figure.text(
            0.012,
            0.012,
            " · ".join(notes),
            fontsize=_NOTE_SIZE,
            color=_NOTE_COLOR,
            ha="left",
            va="bottom",
        )

    # 手工排版而不是 tight_layout()：脚注要占掉底部一条空白，
    # tight_layout 不知道它的存在，会把 x 轴标签压在脚注上。
    figure.subplots_adjust(left=0.125, right=0.975, top=0.885, bottom=0.185)
    return figure


def _draw_bar(axes: Any, points: _Points) -> None:
    """柱状图：唯一的硬规则是**纵轴必须包含 0**。

    为什么这条最重要：只画 `[min, max]` 那一段（"截断纵轴"）会让 101 和 103 看起来
    差一倍 —— 而它们其实只差 2%。这是数据可视化里最常见的误导手法，
    也是本项目的原则（数字不许看起来比实际更惊人）直接禁止的。

    数值标签的取舍：柱状图比的是高度，所以**头尾**最有信息量
    （planner 的聚合默认降序，第一个就是最大值）；柱子超过 12 根时
    每根都写数字会连成一片，只标头尾。
    """
    axes.bar(points.labels, points.values, color=_BAR_COLOR, width=0.62, zorder=3)
    low, high = min(points.values), max(points.values)
    if low >= 0:
        axes.set_ylim(0, high * 1.18 if high > 0 else 1.0)
    elif high <= 0:
        axes.set_ylim(low * 1.18, 0)
    else:
        span = high - low
        axes.set_ylim(low - span * 0.08, high + span * 0.18)
    count = len(points.values)
    indexes = set(range(count)) if count <= _MAX_VALUE_LABELS else {0, count - 1}
    _annotate_values(axes, points, indexes)


def _draw_line(axes: Any, points: _Points) -> None:
    """折线图：点少时把点画出来，点多时只留线。

    单个数据点的折线图是一条看不见的线 —— 用户会以为出图失败了。
    所以 `len == 1` 时把标记放大（标记本身要能被看见），并照样标数值。

    数值标签的取舍和柱状图不同：折线看的是走势，所以标**最高点、最低点、
    首尾各一个**（最多 4 个）—— 这四个读数回答"最高多少、最低多少、从哪到哪"。
    全部相等时（真实的平坦序列）只标一个，否则同一串相同数字会写满整张图。
    """

    low, high = min(points.values), max(points.values)

    def _label_indexes() -> set[int]:
        """折线图只标这四个点：首、尾、最高、最低。

        为什么不给全部点标数值：12 个月的真实序列里 `5,135,498` 和 `5,319,825`
        会挨在一起互相压住（第一版实拍就是这样）。首尾回答"从哪到哪"，
        最高/最低回答"发生了什么"—— 中间那些点的值本来就要回报告表格去核。
        """
        count = len(points.values)
        if count == 1:
            return {0}
        if count <= _LINE_LABEL_ALL_LIMIT:
            return set(range(count))
        if low == high:
            return {0}
        ranked = sorted(range(count), key=lambda index: points.values[index], reverse=True)
        return {0, count - 1, ranked[0], ranked[-1]}

    markersize = 8.0 if len(points.values) == 1 else (5.5 if len(points.values) <= _MAX_VALUE_LABELS else 3.5)
    axes.plot(
        points.labels,
        points.values,
        color=_LINE_COLOR,
        marker="o",
        markersize=markersize,
        linewidth=2.0,
        zorder=3,
    )
    axes.margins(x=0.04)
    if low == high:  # 单点或全部相等：给上下留白，否则线会贴在坐标框上
        pad = abs(low) * 0.2 or 1.0
        axes.set_ylim(low - pad, high + pad)
    else:
        span = high - low
        axes.set_ylim(low - span * 0.18, high + span * 0.18)
    _annotate_values(axes, points, _label_indexes())


def _draw_pie(axes: Any, points: _Points) -> int:
    """饼图：最多 `_MAX_PIE_SLICES` 块，多出来的按数值从小到大合并成「其他」。

    为什么必须合并：18 个分类里最小的那些只占 1%～2%，扇形之间只差几度，
    图上根本分不出边界 —— 那时的饼图不是"信息"，是"彩色圆盘"。
    返回被合并的项数（0 表示没有合并），供脚注使用。
    """
    labels = list(points.labels)
    values = list(points.values)
    merged = 0
    if len(values) > _MAX_PIE_SLICES:
        order = sorted(range(len(values)), key=lambda index: values[index], reverse=True)
        keep = order[: _MAX_PIE_SLICES - 1]
        rest = order[_MAX_PIE_SLICES - 1 :]
        merged = len(rest)
        labels = [labels[index] for index in keep] + [_PIE_OTHER_LABEL]
        values = [values[index] for index in keep] + [sum(values[index] for index in rest)]

    # 刻意不标百分比（`autopct`）：百分比是 matplotlib 现算的，
    # 报告和工具结果里都没有这个数字，标在图上就成了"报告之外的新数字"。
    axes.pie(
        values,
        labels=labels,
        colors=_pie_colors(len(values)),
        textprops={"fontsize": _LABEL_SIZE, "color": _TICK_COLOR},
        wedgeprops={"edgecolor": _SURFACE, "linewidth": 1.2},
    )
    return merged


def _pie_colors(count: int) -> list[str]:
    """饼图配色。

    第一块用品牌主色（和后端 `_BAR_COLOR` 同一个）：单分类或不合并时，
    饼图的"主色"与柱状图一致，看起来才是同一个产品出的图。
    其余颜色按固定顺序取，**顺序固定**才能保证同一份数据两次出图完全一样。

    这 6 个色到底能不能被色盲用户区分，由 `tests/test_visual_palette.py` 用
    真实公式验算（而不是靠"看着挺不一样"）。
    """
    if count == 1:
        return [_BAR_COLOR]
    return list(_PIE_PALETTE)[:count]


def _annotate_values(axes: Any, points: _Points, indexes: set[int]) -> None:
    """在柱顶/点旁直接写数值。

    读数不必让用户回到坐标轴去估 —— 这是"数据可信"最便宜的一步：
    图上写的就是工具算出来的那个数，和报告正文逐字一致。

    **标哪几个点由 `indexes` 决定**（调用方算好），因为柱状图和折线图的取舍不同：
    柱状图比的是高度差，标头尾最有信息量；折线图看的是走势，
    最高点和最低点才是"发生了什么"。

    数值彼此接近时（纵轴为了不截断而从 0 起算，柱子自然就矮）把数字**横排进柱子
    内部**：此时柱顶之间只差几个像素，数字横着写会互相压住 —— 而"N 个数字叠在
    一起"比"数字写在柱子里面"难读得多。文字颜色这时换成白以保证对比度。
    """
    count = len(points.values)
    low, high = min(points.values), max(points.values)
    inside = _values_are_flat(low, high, high - low, count)

    for index, value in enumerate(points.values):
        if index not in indexes:
            continue
        if inside:
            # 竖排写进柱子内部：柱顶只差几像素，只有竖排 + 旋转才放得下。
            axes.annotate(
                _format_value(value),
                xy=(index, value),
                xytext=(0, -6),
                textcoords="offset points",
                ha="center",
                va="top",
                rotation=90,
                fontsize=_LABEL_SIZE,
                color=_SURFACE,
                zorder=5,
            )
        else:
            axes.annotate(
                _format_value(value),
                xy=(index, value),
                xytext=(0, 5 if value >= 0 else -13),
                textcoords="offset points",
                ha="center",
                fontsize=_LABEL_SIZE,
                color=_TICK_COLOR,
                zorder=4,
            )


def _values_are_flat(low: float, high: float, span: float, count: int) -> bool:
    """判断"数值是不是挤在一起"（决定数值标签放柱子内部还是放顶上）。

    判据用**相对于最大值**的跨度，而不是绝对值：
    12 根柱子在 100 万量级上只差 300 时，柱高差异在图上就是看不见的。
    阈值 0.35 的来源：1440px 宽、12 根柱子时，0.35 的跨度差换算到像素上
    刚好是文字高度（约 12px）—— 低于它，横排的文字就会互相压住。
    """
    if count == 1:
        return False
    baseline = max(abs(low), abs(high), 1e-12)
    return span / baseline < 0.35


def _style_cartesian(axes: Any, points: _Points, chart: ChartRequest) -> None:
    """网格、轴线、刻度格式、轴名 —— 所有图型共用的一套版面规则。"""
    axes.set_axisbelow(True)  # 网格必须在柱子后面，否则它会把柱子切开
    axes.yaxis.grid(True, color=_GRID_COLOR, linewidth=0.8)
    axes.xaxis.grid(False)  # 只画横向网格：比较的是高度，竖线只会增加噪声
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(_SPINE_COLOR)
        axes.spines[side].set_linewidth(0.9)
    axes.tick_params(axis="both", labelsize=_TICK_SIZE, colors=_TICK_COLOR, length=3, width=0.8)

    axes.set_ylabel(_axis_label(points.y_field), fontsize=_LABEL_SIZE, color=_TICK_COLOR)
    axes.set_xlabel("序号" if points.use_row_order else _axis_label(points.x_field), fontsize=_LABEL_SIZE, color=_TICK_COLOR)
    bottom, top = axes.get_ylim()
    # 跨度必须在**纵轴范围已经定好之后**取：`_draw_bar`/`_draw_line` 先设 ylim、
    # 再调本函数，所以这里拿到的是最终范围。
    ticks = _nice_ticks(bottom, top)
    if ticks:
        # 刻度自己算，不用 `set_yticks` 之外的任何定位器：
        # 实测过 `MultipleLocator(step)` —— 它给出的刻度**不一定正好是 step 的倍数**，
        # 轴范围 -1～1 时仍然返回 -0.75/-0.25，于是又回到
        # `-0.8 / -0.5 / -0.2` 那种"等距但读起来不等距"的样子。
        axes.set_yticks(ticks)
        step = ticks[1] - ticks[0] if len(ticks) > 1 else 0.0
    else:
        step = 0.0
    axes.yaxis.set_major_formatter(_make_axis_formatter(step))

    labels = _tick_labels(axes)
    if _needs_rotation(labels):
        # 中文标签一多就会互相压住，斜过来写是唯一能看清的办法
        axes.tick_params(axis="x", labelrotation=45)
        for label in axes.get_xticklabels():
            label.set_horizontalalignment("right")


def _tick_labels(axes: Any) -> list[str]:
    """取 X 轴刻度标签的文本（用来判断"要不要斜着写"）。

    判据是**标签总长度**而不是"有没有超过 8 个"：`地区` 这种两个字的标签
    20 个也不挤，`无线耳机第 12 代特别版` 这种 4 个就压住了。原实现只数个数，
    于是"少量超长标签"这一种最容易发生的情况被漏掉了。
    """
    return [str(label.get_text()) for label in axes.get_xticklabels()]


def _needs_rotation(labels: list[str]) -> bool:
    if not labels:
        return False
    if len(labels) > _LABEL_ROTATE_THRESHOLD:
        return True
    longest = max(len(label) for label in labels)
    return len(labels) * longest > 48


def _axis_label(field: str) -> str:
    """轴名：把 `销售额_sum` 这种工具列名写成"销售额（求和）"。

    聚合列的名字是工具拼出来的（`app/tools/aggregate.py` 的 `{metric}_{agg}`），
    下划线裸露在坐标轴上像半成品。这里只做**显示**转换：
    `ChartResult.x_field` / `y_field` 原样返回工具列名，一个字都不改。
    """
    if "_" not in field:
        return field
    metric, _, suffix = field.rpartition("_")
    word = _AGG_WORDS.get(suffix)
    if metric and word:
        return f"{metric}（{word}）"
    return field


# 聚合后缀 → 中文。只覆盖工具真会拼出来的那几个（`app/tools/aggregate.py` 与
# `app/agents/validation.py` 的 `_AGG_SUFFIXES` 是同一份口径）。
_AGG_WORDS = {
    "sum": "求和",
    "mean": "均值",
    "count": "计数",
    "min": "最小",
    "max": "最大",
    "median": "中位数",
}


def _format_value(value: float) -> str:
    """数值直标的写法：整数带千分位，小数保留两位。"""
    if abs(value - round(value)) < 1e-9:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _nice_ticks(bottom: float, top: float) -> list[float]:
    """按"人读得出来"的间距生成纵轴刻度。

    为什么不能交给 matplotlib 的默认定位器：它把轴均分成 4 段，`-1～1` 落成
    `-1 / -0.75 / -0.5 / -0.25 / 0 …`。只写一位小数就变成
    `-1.0 / -0.8 / -0.5 / -0.2 / 0 / 0.2 / 0.5 / 0.8 / 1.0` ——
    刻度是等距的，但**读起来是不等距的**（相邻差在 0.2 和 0.3 之间跳），
    这正是"刻度看起来像坏了"最容易被投诉的形态。

    这里改用 1/2/5 × 10^n 这一族间距，**刻意不含 2.5**：含 2.5 时会选出 0.25
    这种间距，而 0.25 的整数倍写出来是 `0.25 / 0.5 / 0.75`（两位）或
    `0.2 / 0.5 / 0.8`（一位）——前者位数不齐，后者看起来不等距，
    两个都不成立。1/2/5 的倍数永远只有一位有效数字，位数与间距天然对齐。

    并且**把值按间距取整**再交给 matplotlib：取整是必须的 ——
    `0.2 × 3` 在浮点里是 `0.6000000000000001`，不取整就会以这个原值进入
    格式化器，一位小数下印成 `0.6`，与 `0.4` 之间看起来就不齐。

    最后只保留落在轴范围内的刻度，并在轴内补上 0（如果 0 在范围内）——
    柱状图的 0 基线是"不夸大差异"的锚点，缺了它整张图的读法就变了。
    """
    span = top - bottom
    if not math.isfinite(span) or span <= 0:
        return []

    rough = span / 8.0
    exponent = math.floor(math.log10(rough))
    mantissa = rough / (10**exponent)
    step = next(size for size in (1.0, 2.0, 5.0, 10.0) if mantissa <= size) * (10**exponent)
    decimals = max(0, -exponent)
    step = round(step, decimals + 2)

    start = math.ceil(bottom / step - 1e-9)
    stop = math.floor(top / step + 1e-9)
    ticks = [round(index * step, decimals + 2) for index in range(start, stop + 1)]
    if not any(abs(value) < 1e-12 for value in ticks) and bottom < 0 < top:
        ticks.append(0.0)
        ticks.sort()
    return ticks


def _axis_tick(value: float, _position: float | None = None, gap: float = 0.0) -> str:
    """Y 轴刻度写法。

    两种特殊情况先处理，其余按**刻度间距**给小数位：

    - `|v| ≥ 1 亿 / 100 万`：写成 `1.23 亿` / `2,487 万`。原来的 `1e7`
      要求用户自己数零，而"2,487 万"直接可读；
    - 间距 ≥ 1：千分位整数；间距 0.2：一位小数；间距 0.05：两位小数。

    `gap` 是这张图的刻度间距，由 `_make_axis_formatter` 以闭包方式传入，
    不走模块级变量 —— 实测过一版共享变量的写法：同一次进程里连画多张图时，
    格式化器读到的是**上一张图**的间距（`savefig` 才排版文字），
    于是刻度会混着出现 `1.0 / 1 / 0 / 0`。
    """
    if value == 0:
        return "0"
    digits = 0
    if 0 < gap < 1:
        # 间距 0.2 → 1 位，0.05 → 2 位。(1 - gap) 那一点点是为了抵消浮点误差：
        # `math.ceil(-math.log10(0.1))` 正好是 1，但 `-math.log10(0.1)` 在
        # 某些平台上会给出 1.0000000000000002。
        digits = min(4, max(0, math.ceil(-math.log10(gap) - 1e-9)))
    value = round(value, digits)
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= _UNIT_HUNDRED_MILLION:
        return f"{value / _UNIT_HUNDRED_MILLION:,.2f} 亿"
    if magnitude >= _AXIS_UNIT_THRESHOLD:
        return f"{value / _UNIT_TEN_THOUSAND:,.0f} 万"
    return f"{value:,.{digits}f}"


def _make_axis_formatter(gap: float) -> Any:
    """给这张图配一个"知道自己刻度间距"的格式化器。

    为什么要包一层闭包：`FuncFormatter` 只接受 `(value, position)` 两个参数，
    而刻度要不要给小数取决于这张图的刻度间距。共享模块级变量会在
    "一个进程连画多张图"时串味（见 `_axis_tick` 的注释），闭包是唯一干净的办法。
    """
    return FuncFormatter(lambda value, position=None: _axis_tick(value, position, gap))


def _as_number(value: Any) -> float | None:
    """把工具结果里的一个值读成 float；读不了返回 None。

    `bool` 要单独挡掉：Python 里 `True` 就是 `1`，不挡的话"是否含缺失"这种
    布尔字段会被画成一根高度 1 的柱子。NaN / inf 也要挡掉，它们画不出来。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None
