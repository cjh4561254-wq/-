"""`app/agents/analysis.py` 的测试：7 种失败路径 + 成功路径 + 契约把关。

## 为什么这里用**假工具**，不依赖那 7 个真工具

`run_task` 的职责是"校验 → 调用 → 清洗 → 包成 ToolResult"，它不关心工具具体算什么。
用假工具测它有三条实际好处：

1. **测试之间没有隐式依赖**：如果这里依赖"7 个真工具已注册"，
   那么 `app/tools/` 哪天改了名字，这个文件会红得莫名其妙（明明是工具层的改动）。
2. **能造出真工具造不出的场景**：真工具不会返回 `float("nan")`、不会抛 `ValueError`、
   不会返回 `np.float64`，但这三种情况正是"校验 5 到底有没有在把关"的证据。
3. **跑得快**：不用读 `sales.csv`。

假工具用完必须摘干净（`setup_function` 里逐个 `pop`）：注册表是进程内全局状态，
漏摘一个会让 `tests/test_tool_registry.py` 里"工具正好 7 个"的断言失败 ——
这种"跨文件互相污染"是最费时间的一类问题。
"""

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.agents.analysis import run_task
from app.schemas_tool import ToolResult
from app.schemas_workflow import AnalysisTask
from app.tools import registry
from app.tools.registry import ToolSpec

# 两个假工具的名字。加前缀是为了在"注册表里多了个名字"时一眼看出是测试造的
FAKE_OK = "test_fake_ok"
FAKE_BOOM = "test_fake_boom"


def _make_task(tool_name: str = FAKE_OK, params: dict[str, Any] | None = None) -> AnalysisTask:
    """造一个最小任务。默认参数里带一个真实存在的列名 `amount`。"""
    return AnalysisTask(
        task_id="T1",
        order=1,
        kind="aggregate",
        tool_name=tool_name,
        params={"column": "amount"} if params is None else params,
        description="测试用任务",
    )


def _make_df() -> pd.DataFrame:
    """两行三列的小表。列名 `amount` / `region` 被假工具的 schema 引用。"""
    return pd.DataFrame({"amount": [1.0, 2.0], "region": ["华东", "华北"], "note": ["a", "b"]})


def _register_fake_tools() -> None:
    """注册两个假工具，覆盖成功与"工具自己抛异常"两条路。

    schema 里 `column: True` 是给校验层看的标记：校验 3 会拿它的值去 `df.columns` 里找，
    找不到就报 `COLUMN_NOT_FOUND`。`required` 用来触发 `BAD_PARAMS`。
    """

    def fake_ok(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "columns": ["region", "amount"],
            "rows": [{"region": "华东", "amount": 1.0}, {"region": "华北", "amount": 2.0}],
            "row_count": 2,
        }

    def fake_boom(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("炸了")

    def fake_nan(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        return {"x": float("nan")}

    def fake_numpy(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        return {"x": np.float64(1.0)}

    common = {
        "params_schema": {
            "type": "object",
            "properties": {"column": {"type": "str", "column": True}},
            "required": ["column"],
        },
        "category": "aggregate",
    }
    registry.register(ToolSpec(name=FAKE_OK, description="测试用：正常返回", func=fake_ok, **common))
    registry.register(ToolSpec(name=FAKE_BOOM, description="测试用：抛异常", func=fake_boom, **common))
    registry.register(
        ToolSpec(name="test_fake_nan", description="测试用：返回 NaN", func=fake_nan, **common)
    )
    registry.register(
        ToolSpec(name="test_fake_numpy", description="测试用：返回 numpy 标量", func=fake_numpy, **common)
    )


# 所有假工具的名字，teardown 时逐个摘掉
_FAKE_NAMES = (FAKE_OK, FAKE_BOOM, "test_fake_nan", "test_fake_numpy")


def setup_function() -> None:
    _register_fake_tools()


def teardown_function() -> None:
    """把假工具从注册表里摘掉（逐字点名，不用遍历删，免得误删真工具）。"""
    for name in _FAKE_NAMES:
        registry._REGISTRY.pop(name, None)


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_run_task_returns_ok_result() -> None:
    """正常返回 → `ok=True`、`row_count` 来自工具、`columns` 来自工具、耗时非负。"""
    result = run_task(_make_task(), _make_df())

    assert result.ok is True
    assert result.task_id == "T1"
    assert result.tool_name == FAKE_OK
    assert result.row_count == 2
    assert result.columns == ["region", "amount"]
    assert result.data["row_count"] == 2
    assert result.elapsed_ms >= 0
    assert result.error_code is None
    assert result.error_message is None


def test_run_task_elapsed_ms_is_int() -> None:
    """`elapsed_ms` 必须是 `int`：契约上是 `int`（`ge=0`），传浮点会被 Pydantic 拒绝。

    `perf_counter()` 的差值是浮点，所以这里真的会踩到 —— 钉住它，
    免得哪天有人把 `int(round(...))` 改成直接赋值，表现是**每个任务都失败**。
    """
    result = run_task(_make_task(), _make_df())

    assert isinstance(result.elapsed_ms, int)


def test_run_task_columns_falls_back_to_empty_list() -> None:
    """工具没返回 `columns` → `[]`，而不是 `None`（`ToolResult.columns` 是 `list[str]`）。"""

    def fake_no_columns(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        return {"rows": [{"amount": 1.0}]}

    registry.register(
        ToolSpec(
            name="test_fake_no_columns",
            description="测试用：不返回 columns",
            func=fake_no_columns,
            params_schema={
                "type": "object",
                "properties": {"column": {"type": "str", "column": True}},
                "required": ["column"],
            },
            category="aggregate",
        )
    )
    try:
        result = run_task(_make_task("test_fake_no_columns"), _make_df())
    finally:
        registry._REGISTRY.pop("test_fake_no_columns", None)

    assert result.ok is True
    assert result.columns == []
    assert result.row_count == 0  # 取不到就是默认值 0


# ---------------------------------------------------------------------------
# 失败路径：每一类校验各一条
# ---------------------------------------------------------------------------


def test_run_task_rejects_unknown_tool() -> None:
    """校验 1：工具名不在白名单 → `TOOL_NOT_FOUND`。

    这条也是"模型说错工具名"的兜底：模型报什么名字都不会被执行，
    只会在结果里留下一个明确的错误码。
    """
    result = run_task(_make_task("os.system"), _make_df())

    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"
    assert "os.system" in (result.error_message or "")


def test_run_task_rejects_bad_params() -> None:
    """校验 2：缺必填参数 → `BAD_PARAMS`，消息里要点出是哪个键。"""
    result = run_task(_make_task(FAKE_OK, params={}), _make_df())

    assert result.ok is False
    assert result.error_code == "BAD_PARAMS"
    assert "column" in (result.error_message or "")


def test_run_task_rejects_unknown_column() -> None:
    """校验 3：列名不存在 → `COLUMN_NOT_FOUND`，且消息里要出现**真实列名**。

    为什么要断言真实列名出现在消息里：用户看到"列不存在"时，
    下一个问题一定是"那有哪些列？"。把真实列名附在消息里，
    这个错误就能自己回答自己；只写"列不存在"等于把排查工作丢回给用户。
    """
    result = run_task(_make_task(FAKE_OK, params={"column": "不存在的列"}), _make_df())

    assert result.ok is False
    assert result.error_code == "COLUMN_NOT_FOUND"
    assert "不存在的列" in (result.error_message or "")
    assert "amount" in (result.error_message or "")


def test_run_task_catches_tool_exception() -> None:
    """工具自己抛 `ValueError` → 变成 `ok=False`，**绝不允许逃出去**。

    这是"一个任务失败不该让整条工作流崩掉"的最小证据（架构文档 §5.3 / §5.4）。
    """
    result = run_task(_make_task(FAKE_BOOM), _make_df())

    assert result.ok is False
    assert result.error_code == "BAD_RESULT"
    assert "ValueError" in (result.error_message or "")  # 带异常类型名，便于排查


# ---------------------------------------------------------------------------
# 校验 5：返回值必须能进 JSON
# ---------------------------------------------------------------------------


def test_run_task_rejects_nan_in_result() -> None:
    """工具漏了清洗、返回 `float("nan")` → `BAD_RESULT`。

    NaN 本身在 Python 里活得很好，`json.dumps` 默认也会把它写成 `NaN` ——
    而 `NaN` 不是合法 JSON，前端 `JSON.parse` 会直接抛异常。
    所以必须在"结果离开工具层"的地方就拦下来。
    """
    result = run_task(_make_task("test_fake_nan"), _make_df())

    assert result.ok is False
    assert result.error_code == "BAD_RESULT"


def test_run_task_rejects_numpy_scalar_in_result() -> None:
    """工具返回 `np.float64(1.0)` → 同样 `BAD_RESULT`。

    这条比 NaN 更隐蔽：`np.float64(1.0)` 打印出来就是 `1.0`，
    `json.dumps` 在某些版本上还能勉强处理，但 FastAPI 的响应序列化会失败。
    项目铁律是"工具返回值必须是纯 Python 类型"，这里钉住它。
    """
    result = run_task(_make_task("test_fake_numpy"), _make_df())

    assert result.ok is False
    assert result.error_code == "BAD_RESULT"


# ---------------------------------------------------------------------------
# 契约本身在把关（不是"我们碰巧每次都传对了类型"）
# ---------------------------------------------------------------------------


def test_tool_result_rejects_bad_types() -> None:
    """`ToolResult` 的字段约束是真的在生效：类型不对直接抛 `ValidationError`。

    这条钉的不是 `run_task`，而是**契约**。`elapsed_ms` 是 `int`、
    `row_count` 是 `int` 且有 `ge=0` —— 如果哪天有人把契约改成"宽容"的
    （比如 `int | float`），下面两条会立刻变红，而不是等到线上出现
    `elapsed_ms=1.5` 这种谁也没定义过的值。
    """
    common: dict[str, Any] = {
        "task_id": "T1",
        "tool_name": FAKE_OK,
        "ok": True,
        "elapsed_ms": 1,
        "created_at": datetime.now(timezone.utc),
    }

    with pytest.raises(ValidationError):
        ToolResult(**{**common, "elapsed_ms": 1.5})

    with pytest.raises(ValidationError):
        ToolResult(**{**common, "row_count": -1})


def test_run_task_never_raises_on_weird_tool_output() -> None:
    """兜底：工具返回一个根本不是 dict 的东西 → `ok=False`，仍然不抛异常。

    `ToolResult.data` 是 `dict`，直接把 `[1, 2, 3]` 塞进去 Pydantic 会抛
    `ValidationError` —— 而"永远不抛异常"是硬要求，所以这里必须自己拦。
    """

    def fake_list(df: pd.DataFrame, params: dict[str, Any]) -> Any:
        return [1, 2, 3]

    registry.register(
        ToolSpec(
            name="test_fake_list",
            description="测试用：返回 list",
            func=fake_list,
            params_schema={
                "type": "object",
                "properties": {"column": {"type": "str", "column": True}},
                "required": ["column"],
            },
            category="aggregate",
        )
    )
    try:
        result = run_task(_make_task("test_fake_list"), _make_df())
    finally:
        registry._REGISTRY.pop("test_fake_list", None)

    assert result.ok is False
    assert result.error_code == "BAD_RESULT"
