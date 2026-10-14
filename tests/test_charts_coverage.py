"""补 `app/agents/charts.py` 的三个**覆盖缺口**（QA task-29，基线 `a703364`）。

这个文件为什么单独存在
----------------------
这三条断言本来该在 `tests/test_charts.py` 里，但那是别人的文件（同一轮还有写手在飞），
所以按任务书要求写在新文件里，**一行都不改 `tests/test_charts.py`**。
每条都对应一个"把实现改坏以后，原有用例仍然全绿"的实证（变异编号沿用 task-23 的报告）：

1. **m2｜饼图 `value > 0` 改成 `>= 0`，全量 578 passed 一条都没红。**
   原因：`test_pie_drops_non_positive_values` 只喂了负值（-50），**没有喂 0**，
   而 0 正好卡在 `>` 和 `>=` 的唯一分界线上。
2. **m4｜`_DPI = 120` 改成 `121`，全量 0 红。**
   `docs/ARCHITECTURE.md` §5 的 charts.py 契约里白纸黑字写着"保存到
   `data/charts/{workflow_id}/{chart_id}.png`，`dpi=120`" —— 这是**文档承诺过的数字**，
   只是没有任何用例把它和磁盘上的 PNG 对上。所以这里选的是"钉住"而不是"只设下限"：
   断言的是**关系** `实测像素 == round(声明的英寸 × 声明的 dpi)`，
   期望 DPI 写死在测试里（**不去 import 实现里的 `_DPI`**，那是自己和自己比，永远为真）。
3. **M-2｜`test_write_failure_is_raised_not_swallowed` 只有 `pytest.raises(OSError)`，
   函数里 0 个 assert。** 实证：把 `render_chart` 改成进函数就 `raise OSError`，
   那条用例**仍然绿** —— 它区分不了"进函数就炸"和"真写盘才炸"。
   这里补一条有区分力的版本：既钉住异常来源（`match=` 认那句"磁盘满了"），
   也钉住"炸之前已经走到写盘那一步"（目录已建出来、假 savefig 真被调用、画布上已有坐标轴）。

为什么临时目录必须自己钉
------------------------
与 `tests/test_charts.py` 同样的理由：`charts_path` 默认是仓库的 `data/charts/`，
不钉的话会往真实目录写 PNG，断言还会依赖"这台机器上之前跑过什么"。
"""

import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest
from matplotlib.figure import Figure

from app import config
from app.agents.charts import render_chart
from app.schemas_tool import ChartResult, ToolResult
from app.schemas_workflow import ChartRequest

WORKFLOW_ID = "0123456789abcdef0123456789abcdef"

# `docs/ARCHITECTURE.md` §5「charts.py」那张表里"能做"第 ④ 条：
# 「保存到 `data/charts/{workflow_id}/{chart_id}.png`，`dpi=120`」。
# 写死在这里是**故意的** —— 从实现里 import `_DPI` 再和自己比，是永远为真的断言。
EXPECTED_DPI = 120

# 只防"图小到看不清"的下限，不是契约：当前实测 960 × 576，留足余量。
# 契约管的是上面的 dpi，这个下限只挡"退化成缩略图"这种真事故。
_MIN_WIDTH = 600
_MIN_HEIGHT = 400


@pytest.fixture(autouse=True)
def _charts_in_tmp(monkeypatch, tmp_path):
    """把 `CHARTS_DIR` 指到 `tmp_path`，别往仓库的 `data/charts/` 里写。"""
    monkeypatch.setenv("CHARTS_DIR", str(tmp_path / "charts"))
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _result(data: dict, columns: list[str] | None = None, tool_name: str = "group_aggregate") -> ToolResult:
    """造一个工具结果。只填 `render_chart` 真正会读的字段。"""
    return ToolResult(
        task_id="T1",
        tool_name=tool_name,
        ok=True,
        data=data,
        columns=columns if columns is not None else [],
        row_count=len(data.get("rows") or []),
        elapsed_ms=1,
        created_at=datetime.now(timezone.utc),
    )


def _rows_result(rows: list[dict]) -> ToolResult:
    """分组聚合那种形状：`columns` + `rows`。"""
    return _result(
        {"columns": ["地区", "销售额_sum"], "rows": rows, "row_count": len(rows)}
    )


def _pairs_result() -> ToolResult:
    """相关系数那种形状：只有 `pairs`，没有原始数据行（scatter 用）。"""
    pairs = [
        {"x": "销售额", "y": "利润", "r": 0.9872},
        {"x": "销量", "y": "单价", "r": -0.0457},
    ]
    return _result(
        {"columns": ["销售额", "利润"], "matrix": [[1.0, 0.9872]], "pairs": pairs},
        columns=["销售额", "利润"],
        tool_name="correlation",
    )


def _chart(chart_type: str = "bar", x_field: str = "地区", y_field: str = "销售额_sum") -> ChartRequest:
    return ChartRequest(chart_type=chart_type, x_field=x_field, y_field=y_field, title="销售额按地区")


def _path_of(chart: ChartResult) -> Path:
    """按**接口 8 的算法**算路径（不是问 charts.py）。"""
    return config.get_settings().charts_path / WORKFLOW_ID / chart.filename


def _png_size(path: Path) -> tuple[int, int]:
    """从 PNG 的 IHDR 头读真实像素尺寸（纯 stdlib，不引入图像库）。"""
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "这不是一个 PNG 文件"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


# ---------------------------------------------------------------------------
# 缺口 1（m2）：饼图必须丢掉**恰好为 0** 的那一行
# ---------------------------------------------------------------------------


def test_pie_drops_a_row_whose_value_is_exactly_zero():
    """0 和负值一样要被丢掉：`app/agents/charts.py` 的判据是 `value > 0`。

    原有用例只喂了 -50，所以把判据改成 `>= 0` 全量测试一条都不红 —— 0 是 `>` 与 `>=`
    的唯一分界值，不喂它就等于这条边界没人管。这里还带一个**对照**：
    三个正数必须画出三个点。没有对照的话，`point_count == 2` 也可能是"常量写死"，
    探针就失效了。
    """
    rows_with_zero = [
        {"地区": "华南", "销售额_sum": 100.0},
        {"地区": "华北", "销售额_sum": 0.0},
        {"地区": "华东", "销售额_sum": 300.0},
    ]
    chart = render_chart(_rows_result(rows_with_zero), _chart("pie"), WORKFLOW_ID)
    assert chart.point_count == 2, "值为 0 的那一行必须被丢掉（它和负值一起被过滤）"
    assert _path_of(chart).is_file()

    rows_all_positive = [
        {"地区": "华南", "销售额_sum": 100.0},
        {"地区": "华北", "销售额_sum": 200.0},
        {"地区": "华东", "销售额_sum": 300.0},
    ]
    control = render_chart(_rows_result(rows_all_positive), _chart("pie"), WORKFLOW_ID)
    assert control.point_count == 3, "对照：三个正数就该是三个点，否则 == 2 说明不了任何事"


def test_pie_with_only_a_zero_value_raises():
    """只有 0 值的饼图同样画不出来 → 抛异常。

    这是 `> 0` 边界的第二种表现：判据松一格（`>= 0`），一个 0 值就够"画出"一张
    deg 的饼图，异常不再抛，调用方拿到的是一张没有意义的图。原有用例只测了 -100。
    """
    rows = [{"地区": "华南", "销售额_sum": 0.0}]

    with pytest.raises(ValueError, match="至少一个正数"):
        render_chart(_rows_result(rows), _chart("pie"), WORKFLOW_ID)


# ---------------------------------------------------------------------------
# 缺口 2（m4）：磁盘上的 PNG 必须真的是"声明的尺寸 × 声明的 dpi"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chart_type", ["bar", "line", "pie", "scatter"])
def test_saved_png_geometry_matches_the_declared_dpi(monkeypatch, chart_type):
    """`dpi=120` 是 `docs/ARCHITECTURE.md` §5 写明的契约，要能和磁盘上的像素对上。

    为什么是**关系**断言而不是"恰好 960×576"：文档承诺的是 dpi，不是画布英寸数
    （`_FIGURE_SIZE` 是 8.0 × 4.8，但那是实现选的）。所以这里钉两件事：

    - `savefig` 收到的 `dpi` 必须等于文档写死的 `EXPECTED_DPI`（变异 120 → 121 会红）；
    - 真实 PNG 的像素尺寸必须等于 `round(实测英寸 × 声明 dpi)`（两者分家会红，
      比如 savefig 忽略传入 dpi、退回 rcParams 默认的 100）。

    四种图型都过一遍：`_draw` 对四种类型走不同分支，只测 bar 会把"某一类漏传 dpi"放过去。
    """
    calls: list[dict] = []
    real_savefig = Figure.savefig

    def spy_savefig(self, path, **kwargs):
        calls.append({"size_inches": tuple(self.get_size_inches()), "dpi": kwargs.get("dpi")})
        real_savefig(self, path, **kwargs)

    monkeypatch.setattr(Figure, "savefig", spy_savefig)

    if chart_type == "scatter":
        result, x_field, y_field = _pairs_result(), "列对", "相关系数"
    else:
        result = _rows_result(
            [
                {"地区": "华南", "销售额_sum": 100.0},
                {"地区": "华北", "销售额_sum": 200.0},
                {"地区": "华东", "销售额_sum": 300.0},
            ]
        )
        x_field, y_field = "地区", "销售额_sum"

    chart = render_chart(result, _chart(chart_type, x_field, y_field), WORKFLOW_ID)

    assert len(calls) == 1, "savefig 应该只被调用一次"
    assert calls[0]["dpi"] == EXPECTED_DPI, (
        f"{chart_type}：落盘 dpi 必须是文档写明的 {EXPECTED_DPI}，"
        f"实际是 {calls[0]['dpi']}"
    )

    width, height = _png_size(_path_of(chart))
    inches_w, inches_h = calls[0]["size_inches"]
    expected = (round(inches_w * EXPECTED_DPI), round(inches_h * EXPECTED_DPI))
    assert (width, height) == expected, (
        f"{chart_type}：PNG 实际是 {width}×{height}，"
        f"按 {inches_w}×{inches_h} 英寸 @{EXPECTED_DPI}dpi 应该是 {expected[0]}×{expected[1]}"
    )
    assert width >= _MIN_WIDTH and height >= _MIN_HEIGHT, (
        f"{chart_type}：图不能退化成缩略图（实测 {width}×{height}）"
    )


# ---------------------------------------------------------------------------
# 缺口 3（M-2）：写盘失败必须"炸在写盘那一步"，而不是进函数就炸
# ---------------------------------------------------------------------------


def test_write_failure_happens_at_the_write_step_and_leaves_nothing_behind(monkeypatch):
    """写盘失败要抛异常，而且**炸之前必须已经走到写盘那一步**。

    原有的 `test_write_failure_is_raised_not_swallowed` 只有 `pytest.raises(OSError)`：
    把 `render_chart` 改成进函数第一行就 `raise OSError`，它照样绿 ——
    也就是说"出图失败"这个行为里，**失败发生在哪一步**根本没人管。

    这条用例的判据（同时成立才算过）：
    1. 异常信息是 savefig 抛的那句（`match=` 钉住来源，入口就抛的假异常对不上）；
    2. 假 savefig **确实被调用过**（入口就抛的实现在这里露馅）；
    3. 调用时画布上**已经有坐标轴**（说明真的画完了才去写盘）；
    4. 工作流目录**已经建出来**（说明走过了 mkdir，不是半路夭折）；
    5. 失败后目录里**一个 PNG 都不剩**（半个文件比没有文件难查得多）。
    """
    target_dir = config.get_settings().charts_path / WORKFLOW_ID
    assert not target_dir.exists(), "用例开始时目录不该存在（否则第 4 条断言没意义）"

    observed: dict = {}

    def fake_savefig(self, path, **kwargs):
        observed["axes"] = len(self.axes)
        observed["path"] = Path(path)
        observed["path"].write_bytes(b"\x89PNG\r\n\x1a\nhalf written")  # 写了一半
        raise OSError("磁盘满了")

    monkeypatch.setattr(Figure, "savefig", fake_savefig)

    with pytest.raises(OSError, match="磁盘满了"):
        render_chart(
            _rows_result([{"地区": "华南", "销售额_sum": 100.0}]), _chart(), WORKFLOW_ID
        )

    assert observed, "假 savefig 没被调用：说明实现是'还没走到写盘就炸了'"
    assert observed["axes"] >= 1, "写盘之前应该已经画好了坐标轴"
    assert target_dir.is_dir(), "炸之前目录应该已经被建出来（说明走到了写盘这一步）"
    assert not observed["path"].exists(), f"半个文件必须被清掉：{observed['path']}"
    assert list(target_dir.glob("*.png")) == [], "失败后目录里不该留下任何 PNG"
