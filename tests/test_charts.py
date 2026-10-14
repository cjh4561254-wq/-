"""`app/agents/charts.py` 里 `render_chart()` 的测试 —— 出图。

这个文件在测什么
----------------
`render_chart()` 是唯一会**往磁盘写文件**的 Agent：它把工具算好的数字变成一张 PNG，
再回一个前端能直接塞进 `<img src>` 的 URL。所以它有三类会"悄悄坏掉"的地方，
每一类都在这里钉住：

1. **图里画的不是工具算的那批数**。图表只从 `ToolResult.data` 取数（§3.4），
   一旦它自己重新分组、或者把缺失值当 0 补齐，就会出现"报告写 2487 万、图上 2500 万"
   这种两个数都"对"但算的不是同一批行的 bug。这里逐点核对 `point_count` 与行数。
2. **中文变成方框**。matplotlib 找不到中文字体时**不报错**，只是每个汉字画成一个方框，
   只有人眼看截图才会发现。所以这里断言的是 `rcParams` 里的字体设置（可失败的事实），
   而不是"我看了一眼图没方框"。
3. **落盘路径和接口对不上**。前端拿到 `chart.url` 直接请求接口 8，路由按
   `charts_dir / workflow_id / filename` 找文件。两边算的必须是同一个地方，
   拼错的表现是图片裂开而后端日志一切正常。

为什么临时目录必须自己钉
------------------------
`charts_path` 默认是仓库的 `data/charts/`。不钉的话这些用例会往真实目录里写 PNG，
而且断言会依赖"这台机器上之前跑过什么"。所以 `CHARTS_DIR` 一律指到 `tmp_path`。

为什么断言的是"文件真的在磁盘上"
--------------------------------
`render_chart` 返回一个 `ChartResult` 只说明它**认为自己**写成功了。
用户能不能看到图，取决于那个文件在不在。所以下面每个用例都回读磁盘，
而不是只看返回值 —— `path.is_file()` 和 `path.stat().st_size > 0` 是两条不同的断言：
前者防"没写"，后者防"写了个 0 字节的空文件"（前端显示裂图，比没有图更难查）。
"""

import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import config
from app.agents.charts import render_chart
from app.schemas_tool import ChartResult, ToolResult
from app.schemas_workflow import ChartRequest

WORKFLOW_ID = "0123456789abcdef0123456789abcdef"

# 图表标题里刻意放中文：字体没配好时它会变成方框，而这条断言不会因此变红
# （方框也是字符），所以真正的字体断言在 test_chinese_font_is_configured 里。
CHINESE_TITLE = "销售额按地区"


@pytest.fixture(autouse=True)
def _charts_in_tmp(monkeypatch, tmp_path):
    """把 `CHARTS_DIR` 指到 `tmp_path`，别往仓库的 `data/charts/` 里写。"""
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _result(
    data: dict,
    columns: list[str] | None = None,
    task_id: str = "T1",
    tool_name: str = "group_aggregate",
    ok: bool = True,
) -> ToolResult:
    """造一个工具结果。只填 `render_chart` 真正会读的字段。"""
    return ToolResult(
        task_id=task_id,
        tool_name=tool_name,
        ok=ok,
        data=data,
        columns=columns if columns is not None else [],
        row_count=len(data.get("rows") or []),
        elapsed_ms=1,
        created_at=datetime.now(timezone.utc),
    )


def _rows_result() -> ToolResult:
    """分组聚合那种形状：`columns` + `rows`。"""
    rows = [
        {"地区": "华南", "销售额_sum": 24871657.0},
        {"地区": "华北", "销售额_sum": 18234567.0},
        {"地区": "华东", "销售额_sum": 9102334.5},
    ]
    return _result({"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": 3})


def _pairs_result() -> ToolResult:
    """相关系数那种形状：只有 `pairs`，没有原始数据点。"""
    pairs = [
        {"x": "销售额", "y": "利润", "r": 0.9872},
        {"x": "销量", "y": "单价", "r": -0.0457},
    ]
    return _result(
        {"columns": ["销售额", "利润"], "matrix": [[1.0, 0.9872]], "pairs": pairs},
        columns=["销售额", "利润"],
        tool_name="correlation",
    )


def _chart(chart_type: str = "bar", x_field: str = "地区", y_field: str = "销售额_sum"):
    return ChartRequest(
        chart_type=chart_type,
        x_field=x_field,
        y_field=y_field,
        title=CHINESE_TITLE,
    )


def _path_of(chart: ChartResult) -> Path:
    """按**接口 8 的算法**算出这张图应该在哪儿（不是问 charts.py）。"""
    return config.get_settings().charts_path / WORKFLOW_ID / chart.filename


# ---------------------------------------------------------------------------
# 1. 四种图各出一条真 PNG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chart_type", ["bar", "line", "pie", "scatter"])
def test_every_chart_type_writes_a_real_png(chart_type):
    """四种 `chart_type` 都要能落出一张非空的 PNG。

    参数化而不是写四个函数：`ChartType` 是契约里的 4 值 Literal，漏掉一种
    就等于有一类图从来没人跑过。
    """
    result = _pairs_result() if chart_type == "scatter" else _rows_result()
    x_field, y_field = ("列对", "相关系数") if chart_type == "scatter" else ("地区", "销售额_sum")

    chart = render_chart(result, _chart(chart_type, x_field, y_field), WORKFLOW_ID)

    path = _path_of(chart)
    assert path.is_file(), f"{chart_type} 图应该真的写在磁盘上：{path}"
    assert path.stat().st_size > 0, f"{chart_type} 图不能是 0 字节"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "落盘的必须是真 PNG"


def test_chart_result_carries_the_frozen_fields():
    """返回值的 10 个字段都要填对（少一个前端就是 `undefined`）。"""
    chart = render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert chart.chart_id and len(chart.chart_id) == 12
    assert chart.filename == f"{chart.chart_id}.png"
    assert chart.title == CHINESE_TITLE
    assert chart.chart_type == "bar"
    assert chart.url == f"/api/workflows/{WORKFLOW_ID}/charts/{chart.filename}"
    assert chart.source_task_id == "T1"
    assert chart.x_field == "地区"
    assert chart.y_field == "销售额_sum"
    assert chart.point_count == 3
    assert chart.created_at.tzinfo is not None, "时间戳要带时区（项目统一 UTC）"


def test_two_charts_do_not_overwrite_each_other():
    """同一个工作流画两张图 → 两个不同的文件名，互不覆盖。

    `chart_id` 用 `uuid4().hex[:12]` 而不是序号，就是为了这件事：并发或重跑时
    序号会撞，而 UUID 不会。
    """
    first = render_chart(_rows_result(), _chart(), WORKFLOW_ID)
    second = render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert first.chart_id != second.chart_id
    assert _path_of(first).is_file() and _path_of(second).is_file()


# ---------------------------------------------------------------------------
# 2. 中文标题不乱码
# ---------------------------------------------------------------------------


def test_chinese_font_is_configured():
    """字体必须**显式**配好，否则中文标题会画成方框 —— 而且不报错。

    matplotlib 找不到汉字时的表现是"每个字一个方框"，它不会抛异常、不会打日志，
    只有人眼看截图才发现。所以这里断言 `rcParams` 这个**可失败的事实**，
    而不是"我看了一眼没方框"。`axes.unicode_minus` 同理：微软雅黑没有 U+2212
    这个减号字形，负数轴上的负号也会变方框。
    """
    import matplotlib.pyplot as plt

    assert "Microsoft YaHei" in plt.rcParams["font.sans-serif"]
    assert plt.rcParams["axes.unicode_minus"] is False


def test_backend_is_agg():
    """服务器上没有显示器，必须用 `Agg` 后端（否则导入时就会尝试开窗口）。"""
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"


# ---------------------------------------------------------------------------
# 3. 画的是工具算出来的那些数，一个不多一个不少
# ---------------------------------------------------------------------------


def test_point_count_equals_the_number_of_rows():
    """`point_count` 必须等于真正画上去的点数（这里是行数）。

    这是"图与报告同源"的核心断言：报告里的 `24,871,657.00` 和图上最高的那根柱子
    必须来自同一行。点数对不上，说明中间又算了一遍或丢了几行。
    """
    chart = render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert chart.point_count == 3


def test_top_n_single_column_uses_row_order_and_counts_every_row():
    """`top_n` 只返回一列时，x 轴用行号，`point_count` 仍然等于行数。

    `top_n` 不传 `columns` 时只返回"排序列"这一列（见 `app/tools/aggregate.py`），
    所以 x_field 和 y_field 会是同一个名字。这时候 x 轴没有可用的标签，
    只能用行号 —— 但**点数不能因此变成 0**：图上应该有几根柱子就有几个点。
    """
    rows = [{"销售额": value} for value in (14999850.0, 12000000.0, 8000000.0)]
    result = _result(
        {"columns": ["销售额"], "rows": rows, "row_count": 3}, tool_name="top_n"
    )

    chart = render_chart(result, _chart("bar", "销售额", "销售额"), WORKFLOW_ID)

    assert chart.x_field == chart.y_field == "销售额"
    assert chart.point_count == 3
    assert _path_of(chart).is_file()


def test_rows_with_missing_values_are_skipped_not_treated_as_zero():
    """缺失值跳过，不当 0 —— 缺失不等于零。

    当成 0 会在图上留一根贴地的柱子，看起来像"这一组的销售额是 0"，
    而实际是"这一组没有这个数"。两者在业务上完全是两回事。
    """
    rows = [
        {"地区": "华南", "销售额_sum": 100.0},
        {"地区": "华北", "销售额_sum": None},
        {"地区": "华东", "销售额_sum": 300.0},
    ]
    result = _result({"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": 3})

    chart = render_chart(result, _chart(), WORKFLOW_ID)

    assert chart.point_count == 2, "缺一个值就少一个点，不能补成 0"


def test_all_values_missing_raises_instead_of_drawing_a_fake_bar():
    """整列都没有可画的数 → 抛异常，而不是画一根假柱子。

    画一根高度 0 的柱子等于告诉用户"这一组的量是 0"，而真相是"这一列不是数值列"。
    宁可没有图，也不能有一张说谎的图。
    """
    rows = [{"地区": "华南", "销售额_sum": "没有数"}, {"地区": "华北", "销售额_sum": None}]
    result = _result({"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": 2})

    with pytest.raises(ValueError, match="没有可以画的数值"):
        render_chart(result, _chart(), WORKFLOW_ID)


def test_result_without_rows_or_pairs_raises():
    """工具结果里既没有 `rows` 也没有 `pairs`（如 `missing_values`）→ 抛异常。

    `missing_values` 的 `columns` 是"每列的缺失个数"，画出来绝大多数是 0，
    看不出信息，所以计划阶段就不给它配图（见 `planner._chart_for`）。
    真有人给它配了图，这里必须明确失败而不是画一张空图。
    """
    result = _result(
        {"row_count": 2168, "duplicate_row_count": 8, "columns": []},
        tool_name="missing_values",
    )

    with pytest.raises(ValueError, match="没有可以画图的数据行"):
        render_chart(result, _chart(), WORKFLOW_ID)


def test_boolean_column_is_not_drawn_as_a_number():
    """布尔列不能当数字画（`True` 在 Python 里就是 1）。

    否则"是否含缺失"这种列会被画成一根高度 1 的柱子，看起来像一个真实的数量。
    """
    rows = [{"地区": "华南", "是否异常": True}, {"地区": "华北", "是否异常": False}]
    result = _result({"columns": ["地区", "是否异常"], "rows": rows, "row_count": 2})

    with pytest.raises(ValueError, match="没有可以画的数值"):
        render_chart(result, _chart("bar", "地区", "是否异常"), WORKFLOW_ID)


def test_pie_drops_non_positive_values():
    """饼图丢非正值（matplotlib 画不了负的扇区），并因此少一个点。"""
    rows = [
        {"地区": "华南", "销售额_sum": 100.0},
        {"地区": "华北", "销售额_sum": -50.0},
        {"地区": "华东", "销售额_sum": 300.0},
    ]
    result = _result({"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": 3})

    chart = render_chart(result, _chart("pie"), WORKFLOW_ID)

    assert chart.point_count == 2


def test_pie_without_any_positive_value_raises():
    """全非正数 → 抛异常（一张画不出扇区的饼图没有意义）。"""
    rows = [{"地区": "华南", "销售额_sum": -100.0}]
    result = _result({"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": 1})

    with pytest.raises(ValueError, match="至少一个正数"):
        render_chart(result, _chart("pie"), WORKFLOW_ID)


def test_correlation_pairs_are_drawn_from_pairs_not_recomputed():
    """相关系数的结果只有 `pairs`（没有原始数据点）→ 从 `pairs` 造两列来画。

    这条也证明了图表**不会自己去读原始 df 重算相关系数**（§3.4 不能做②）：
    它拿到的只有 `ToolResult`，`pairs` 里是什么就画什么。
    """
    chart = render_chart(
        _pairs_result(), _chart("bar", "列对", "相关系数"), WORKFLOW_ID
    )

    assert chart.point_count == 2
    assert chart.x_field == "列对"
    assert chart.y_field == "相关系数"
    assert _path_of(chart).is_file()


# ---------------------------------------------------------------------------
# 4. 字段名对不上时的兜底（模型会写错一个字）
# ---------------------------------------------------------------------------


def test_unknown_y_field_falls_back_to_the_last_real_column():
    """请求的 y 字段不存在 → 用工具实际返回的最后一列，并且不抛异常。

    模型填错字段名是常态（"销售额"写成"销售金额"）。硬取的结果是 `KeyError`
    → 调用方记一条"出图失败"，而真正的原因是"字段名写错了一个字" ——
    排查成本高得多。这里退一步用真实列名，图仍然画得出来。
    """
    chart = render_chart(_rows_result(), _chart("bar", "地区", "销售金额"), WORKFLOW_ID)

    assert chart.y_field == "销售额_sum", "应该退回工具实际返回的列"
    assert _path_of(chart).is_file()


def test_unknown_x_field_falls_back_to_the_first_real_column():
    """x 字段对不上 → 用第一列。"""
    chart = render_chart(_rows_result(), _chart("bar", "大区", "销售额_sum"), WORKFLOW_ID)

    assert chart.x_field == "地区"
    assert _path_of(chart).is_file()


def test_reported_fields_are_the_ones_actually_used():
    """`ChartResult` 里记的必须是**实际用到的**字段，不是请求里那个。

    否则前端拿 `x_field` 去表里找列会找不到 —— 图上画的是 A，元数据写着 B。
    """
    chart = render_chart(_rows_result(), _chart("bar", "不存在", "也不存在"), WORKFLOW_ID)

    assert chart.x_field == "地区"
    assert chart.y_field == "销售额_sum"
    assert "不存在" not in (chart.x_field, chart.y_field)


# ---------------------------------------------------------------------------
# 5. 落盘路径与失败行为
# ---------------------------------------------------------------------------


def test_file_lands_where_interface_8_looks_for_it():
    """落盘路径必须和接口 8 的算法一致：`charts_path / workflow_id / filename`。

    这条断言**自己算路径**（不问 charts.py 要），所以两边算法一旦分家就会红。
    """
    chart = render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    expected_dir = config.get_settings().charts_path / WORKFLOW_ID
    assert expected_dir.is_dir(), "工作流目录应该被建出来"
    assert (expected_dir / f"{chart.chart_id}.png").is_file()
    assert _path_of(chart).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_directory_is_created_when_missing():
    """目录不存在时要自己建（接口 8 明确不 mkdir，见 routes_workflows.py）。"""
    target = config.get_settings().charts_path / WORKFLOW_ID
    assert not target.exists(), "用例开始时目录不该存在"

    chart = render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert target.is_dir()
    assert _path_of(chart).is_file()


def test_write_failure_is_raised_not_swallowed():
    """写盘失败必须抛异常，不能返回一个指向不存在文件的 `ChartResult`。

    调用方（`orchestrator._render_charts`）靠这个异常来跳过这张图并记日志；
    如果这里返回一个对象，用户就会拿到一个死链 —— 看起来像后端坏了。
    造法：把 `CHARTS_DIR` 指到一个**文件**（不是目录），`mkdir` 必然失败。
    """
    blocker = config.get_settings().charts_path
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
    config.reset_settings_cache()

    with pytest.raises(OSError):
        render_chart(_rows_result(), _chart(), WORKFLOW_ID)


def test_no_half_written_file_is_left_behind(monkeypatch):
    """画图中途失败 → 不留半个文件（`target.unlink(missing_ok=True)`）。

    半个 PNG 比没有 PNG 难查得多：接口 8 会 200 返回它，前端显示裂图，
    而后端日志里一切正常。

    造法要能让**文件先存在再失败**：直接让 `mkdir` 失败是不够的 ——
    那样文件从来没被创建过，`unlink` 是不是真的被调用根本没测到。
    所以这里换掉 `savefig`，让它先写几个字节（模拟磁盘写了一半）再抛异常，
    然后断言那半个文件确实被清掉了。
    """
    from matplotlib.figure import Figure

    written: list[Path] = []

    def fake_savefig(self, path, **kwargs):
        target = Path(path)
        target.write_bytes(b"\x89PNG\r\n\x1a\nhalf written")  # 写了一半
        written.append(target)
        raise OSError("磁盘满了")

    monkeypatch.setattr(Figure, "savefig", fake_savefig)

    with pytest.raises(OSError, match="磁盘满了"):
        render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert written, "假 savefig 应该被调用过（否则这条用例什么都没测）"
    assert not written[0].exists(), f"半个文件必须被清掉：{written[0]}"


def test_a_failed_chart_does_not_leave_the_directory_behind_as_a_file():
    """写盘失败时，`charts_path` 那个路径本身不该被破坏。"""
    blocker = config.get_settings().charts_path
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("x", encoding="utf-8")
    config.reset_settings_cache()

    with pytest.raises(OSError):
        render_chart(_rows_result(), _chart(), WORKFLOW_ID)

    assert blocker.is_file(), "失败路径不该把那个文件删掉或改成目录"
