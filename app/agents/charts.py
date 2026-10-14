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

logger = logging.getLogger(__name__)

# 出图分辨率。120 是"网页里够清晰、文件又不至于几 MB"的折中：
# 默认的 100 在高分屏上偏糊，200 会让一张 20 根柱子的图超过 500 KB。
_DPI = 120

# 画布尺寸（英寸）。8×4.8 是 4:3 偏宽的常见比例，20 个中文分组标签也放得下。
_FIGURE_SIZE = (8.0, 4.8)

# 柱子/线条的颜色。固定一个颜色而不是让 matplotlib 每次循环换色：
# 同一份数据两次生成的图看起来必须一样，否则"报告截图"和"网页上的图"会对不上。
_BAR_COLOR = "#3b6ea5"
_LINE_COLOR = "#c0504d"

# X 轴标签超过这个数量就斜着写，否则中文标签会互相压在一起变成一团黑。
_LABEL_ROTATE_THRESHOLD = 8

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
    """按 `chart_type` 画图。只做"摆位置"，不做任何聚合。"""
    figure, axes = plt.subplots(figsize=_FIGURE_SIZE)

    if chart.chart_type == "bar":
        axes.bar(points.labels, points.values, color=_BAR_COLOR)
    elif chart.chart_type == "line":
        axes.plot(points.labels, points.values, color=_LINE_COLOR, marker="o")
    elif chart.chart_type == "pie":
        # 刻意不标百分比（`autopct`）：百分比是 matplotlib 现算的，
        # 报告和工具结果里都没有这个数字，标在图上就成了"报告之外的新数字"。
        axes.pie(points.values, labels=points.labels)
    else:  # scatter —— ChartType 只有四个取值，走到这里只可能是散点
        axes.scatter(points.x_values, points.values, color=_BAR_COLOR)

    axes.set_title(chart.title)

    if chart.chart_type != "pie":
        axes.set_ylabel(points.y_field)
        if points.use_row_order:
            axes.set_xlabel("序号")
        else:
            axes.set_xlabel(points.x_field)
        if len(points.labels) > _LABEL_ROTATE_THRESHOLD:
            # 中文标签一多就会互相压住，斜过来写是唯一能看清的办法
            axes.tick_params(axis="x", labelrotation=45)
            for label in axes.get_xticklabels():
                label.set_horizontalalignment("right")

    figure.tight_layout()  # 不调的话长标题会被裁掉一半
    return figure


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
