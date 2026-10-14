"""D5 契约测试：把 D5 两个还没写的函数签名、三个模型的字段清单钉住。

和 `tests/test_contracts_d4.py` 同一个思路（**棘轮**）：

    - 模块/函数还不存在 → skip（D5 开工前这是正常的）
    - 存在但签名/字段与契约不一致 → fail（说明有人改了约定，必须回来看这份契约）

依据：`docs/ARCHITECTURE.md` §3.4（`render_chart`）、§3.5.1（`validate`）、
§3.5.2（`build_report`）、§4.8（`ChartResult`）、§4.9（`ValidationResult`）、
§4.10（`FinalReport`）。

**D5 结束时这里的 skip 必须归零** —— 这是可数的验收判据，不是"感觉写完了"。

### 两处 Lead 拍板（架构文档内部有矛盾，以本文件为准）

1. **`render_chart(tool_result, chart, workflow_id)`**，不是"拆成 4 个标题/坐标轴参数"，
   更**不是** `render_chart(df, ...)`。§3.4 的输入表列的是 `tool_result` +
   `chart_type/title/x_field/y_field`，而那四个恰好就是 `ChartRequest` 的全部字段，
   所以收成 `chart: ChartRequest` 一个参数。§8.2.2 里那句
   `render_chart(df, task.chart, ...)` 传了原始 `df` 是**笔误**：§3.4 的"不能做②"
   明写"不读原始 `df`"，而"图只能画已经算好的数字"正是本项目的卖点。
2. **"写文件"与"登记进 `state.charts`"都在 Orchestrator 的 charting 阶段连续完成**，
   **不在 `app/agents/analysis.py`**。§8.2.2 说由 `analysis.py` 登记，与 §3.3 的
   "不能做③：不修改 `WorkflowState`"直接冲突；而 `analysis.run_task(task, df)` 的签名里
   根本没有 `state`。规则的本意是"两件事必须紧挨着发生、不许分开"，而不是"必须写在哪个文件"。
   Orchestrator 是 `state` 的唯一所有者，所以落点在它那儿。
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

# 契约里的函数签名（参数名 / 顺序 / 默认值）。
# 只钉"调用方看得见的部分"：参数名（调用方按名字传）、顺序、默认值。
_CONTRACT_PARAMS: dict[tuple[str, str], list[tuple[str, object]]] = {
    ("app.agents.charts", "render_chart"): [
        ("tool_result", inspect.Parameter.empty),
        ("chart", inspect.Parameter.empty),
        ("workflow_id", inspect.Parameter.empty),
    ],
    ("app.agents.validation", "validate"): [("state", inspect.Parameter.empty)],
    # `validation` 有默认值 `None`：第一次写报告时还没有校验结果（先有报告才谈得上
    # 校验报告里的数字），重写那一轮才把 `ValidationResult` 传进来（§3.5.2 的重写路径）。
    ("app.agents.validation", "build_report"): [
        ("state", inspect.Parameter.empty),
        ("validation", None),
    ],
}

# §4.8 / §4.9 / §4.10 的字段清单，逐个写死。
# 为什么用**集合相等**而不是"包含"：少一个字段就是前端拿到 `undefined`，
# 多一个字段就是有人偷偷扩了契约 —— 两种都要红。
_CHART_RESULT_FIELDS = {
    "chart_id",
    "filename",
    "title",
    "chart_type",
    "url",
    "source_task_id",
    "x_field",
    "y_field",
    "point_count",
    "created_at",
}
_VALIDATION_RESULT_FIELDS = {
    "passed",
    "checked_numbers",
    "unsupported_numbers",
    "issues",
    "checked_at",
}
_FINAL_REPORT_FIELDS = {
    "report_id",
    "title",
    "summary",
    "sections",
    "data_quality_notes",
    "citations",
    "generated_at",
    "llm_used",
}

_VALIDATION_ISSUE_CODES = {
    "UNSUPPORTED_NUMBER",
    "UNKNOWN_COLUMN",
    "EMPTY_RESULT",
    "FAILED_TASK",
    "MISSING_CHART_FILE",
    "SHORT_REPORT",
}

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _resolve(dotted_module: str, func_name: str):
    """取函数；模块或函数不存在时返回 None（不 import 失败，避免收集错误）。"""
    try:
        module = __import__(dotted_module, fromlist=[func_name])
    except ImportError:
        return None
    return getattr(module, func_name, None)


def _function_ast(module_path: str, func_name: str) -> ast.FunctionDef | None:
    """在源码文件里找某个顶层函数的 AST 节点；找不到返回 None。"""
    source_file = _PROJECT_ROOT / Path(*module_path.split(".")).with_suffix(".py")
    if not source_file.is_file():
        return None
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    return None


@pytest.mark.parametrize(("target", "expected"), sorted(_CONTRACT_PARAMS.items()))
def test_d5_function_signatures_match_the_frozen_contract(target, expected):
    """两个 D5 函数的参数名/顺序/默认值必须与 §3.4 / §3.5 一致。

    为什么钉参数名而不只钉个数：调用方是**按关键字**调用的
    （`render_chart(tool_result=..., chart=..., workflow_id=...)`），
    名字变了就是破坏契约，哪怕位置和个数都一样。
    """
    dotted_module, func_name = target
    func = _resolve(dotted_module, func_name)
    if func is None:
        pytest.skip(f"{dotted_module}.{func_name} 还没写（D5 施工中）")

    actual = [
        (p.name, p.default) for p in inspect.signature(func).parameters.values()
    ]
    assert actual == expected, f"{dotted_module}.{func_name} 签名偏离契约"


def test_chart_result_field_set_is_frozen_exactly():
    """`ChartResult` 的字段必须**恰好**是 §4.8 那 10 个。

    这 10 个字段同时被接口 6、接口 8、前端三处使用（A-2 拍板），
    任何一处自己拼一份都会让 `url` 与 `filename` 对不上，而**图片 404 不会让测试变红**。
    """
    from app.schemas_tool import ChartResult

    assert set(ChartResult.model_fields) == _CHART_RESULT_FIELDS


def test_validation_result_field_set_is_frozen_exactly():
    from app.schemas_workflow import ValidationResult

    assert set(ValidationResult.model_fields) == _VALIDATION_RESULT_FIELDS


def test_final_report_field_set_is_frozen_exactly():
    from app.schemas_workflow import FinalReport

    assert set(FinalReport.model_fields) == _FINAL_REPORT_FIELDS


def test_validation_issue_codes_are_frozen_exactly():
    """`ValidationIssue.code` 的取值必须恰好是 §4.9 的 6 个。

    校验结果是"给人和前端看的证据"，码一旦漂移，前端的分支就会静默走不到。
    """
    from typing import get_args

    from app.schemas_workflow import ValidationIssue

    annotation = ValidationIssue.model_fields["code"].annotation
    assert set(get_args(annotation)) == _VALIDATION_ISSUE_CODES


def test_chart_result_rejects_filename_url_mismatch():
    """`ChartResult` 自带的一致性校验必须真的会炸（§4.8 的"会报错的代码"）。

    这条**不是**在测新功能，而是在测"D5 实现不会把这条护栏拆掉"：
    把 `chart_id` 与 `filename` 解绑、或让 `url` 与 `filename` 不一致时，
    构造对象的那一刻就必须抛 `ValidationError`。
    """
    import datetime

    import pydantic
    from app.schemas_tool import ChartResult

    good = dict(
        chart_id="a1b2c3d4e5f6",
        filename="a1b2c3d4e5f6.png",
        title="销售额按地区",
        chart_type="bar",
        url="/api/workflows/" + "0" * 32 + "/charts/a1b2c3d4e5f6.png",
        source_task_id="T1",
        x_field="地区",
        y_field="销售额",
        point_count=6,
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    )
    ChartResult(**good)  # 合法组合必须能构造出来

    with pytest.raises(pydantic.ValidationError):
        ChartResult(**{**good, "filename": "deadbeef0000.png"})

    with pytest.raises(pydantic.ValidationError):
        ChartResult(**{**good, "url": "/api/workflows/" + "0" * 32 + "/charts/other.png"})


def test_render_chart_does_not_touch_the_registry():
    """`render_chart` 不许 import / 使用 `app.core.registry`（§3.4"不能做"）。

    它一旦能改 `state`，"写文件"和"登记"就会分家 —— 磁盘上留下孤儿 PNG，
    而接口 8 按 `chart_id` 校验时 404，前端看到裂图。
    用 AST 扫，不靠 import 副作用（与 `test_contracts_d4.py` 里
    `run_workflow` 不许 import fastapi 是同一个手法）。
    """
    node = _function_ast("app.agents.charts", "render_chart")
    if node is None:
        pytest.skip("app/agents/charts.py 还没写（D5 施工中）")

    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert "registry" not in names, "render_chart 不许碰 registry"
    assert "state" not in names, "render_chart 不许碰 state（登记由调用方做）"


def test_validate_is_pure_python_and_never_asks_the_model():
    """`validate` 不许调用模型（§3.5.1"调用 LLM：否"）。

    数字溯源是**防编造**的那道防线。如果它自己也要模型参与，
    "编造"就从一个模型的手里转到了另一个模型的手里 —— 这道防线必须能被
    "离线、纯代码、可手算复现"地跑一遍。
    """
    node = _function_ast("app.agents.validation", "validate")
    if node is None:
        pytest.skip("app/agents/validation.py 的 validate 还没写（D5 施工中）")

    source = textwrap.dedent(ast.unparse(node))
    for forbidden in ("client.chat", ".chat(", "llm_is_configured", "from app.llm"):
        assert forbidden not in source, f"validate 里出现了 {forbidden!r}，它必须是纯代码"
