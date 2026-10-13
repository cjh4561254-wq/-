"""`app/tools/registry.py` 的测试：白名单、schema、OpenAI tools 翻译。

为什么这些断言值得写
--------------------
注册表是"模型能用什么"的唯一边界。它错了两类事都会很难查：
- **白名单漏了** → 模型报的工具全部 `TOOL_NOT_FOUND`，看起来像"模型乱报名字"；
- **schema 翻译错** → 模型拿到的参数说明和我们的校验不一致，它按说明传、我们照校验拒。

所以这里既断言"有几个、叫什么"，也断言"翻译成 OpenAI 格式以后长什么样"
（`column` / `min_items` 这些内部标记绝不能漏给模型）。
"""

from dataclasses import FrozenInstanceError
from typing import Any

import pandas as pd
import pytest

from app.tools import registry
from app.tools.registry import ToolSpec

# 7 个工具的名字（排序后）。写死是有意的：工具变多必须有人来改这一行
TOOL_NAMES = [
    "compare_groups",
    "correlation",
    "describe_dataset",
    "group_aggregate",
    "missing_values",
    "time_series_summary",
    "top_n",
]

CATEGORIES = {"profile", "aggregate", "rank", "quality", "compare", "trend"}

# 哪些工具需要画图（D5/D6 会看这个字段）
CHART_TOOLS = {
    "group_aggregate",
    "top_n",
    "compare_groups",
    "correlation",
    "time_series_summary",
}


def test_register_all_registers_exactly_seven_tools() -> None:
    registry.register_all()
    assert registry.list_names() == TOOL_NAMES


def test_register_all_is_idempotent() -> None:
    """连调两次不能报错、也不能登记出重复项（路由/Agent 层可能各调一次）。"""
    registry.register_all()
    registry.register_all()
    assert registry.list_names() == TOOL_NAMES


def test_get_returns_the_spec_for_a_known_tool() -> None:
    spec = registry.get("group_aggregate")
    assert spec is not None
    assert spec.name == "group_aggregate"
    assert spec.func is not None


@pytest.mark.parametrize("name", ["os.system", "eval", "open", "__import__", "describe", ""])
def test_get_returns_none_for_anything_outside_the_whitelist(name: str) -> None:
    """白名单外一律 None：**没有 getattr、没有 eval**，模型报什么都取不到东西。"""
    assert registry.get(name) is None


def test_list_specs_matches_list_names_and_is_sorted() -> None:
    specs = registry.list_specs()
    assert [spec.name for spec in specs] == registry.list_names()
    assert [spec.name for spec in specs] == sorted(spec.name for spec in specs)


def test_registering_a_duplicate_name_raises() -> None:
    spec = registry.get("top_n")
    assert spec is not None
    with pytest.raises(ValueError) as excinfo:
        registry.register(spec)
    assert "工具名重复" in str(excinfo.value)


def test_tool_spec_is_frozen() -> None:
    """登记后不该再被改：改一个字段就绕过了"代码即白名单"的保证。"""
    spec = registry.get("top_n")
    assert spec is not None
    with pytest.raises(FrozenInstanceError):
        spec.needs_chart = True  # type: ignore[misc]


def test_to_openai_tools_shape() -> None:
    tools = registry.to_openai_tools()
    assert len(tools) == 7
    for tool in tools:
        assert tool["type"] == "function"
        function = tool["function"]
        assert function["name"] in TOOL_NAMES
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["properties"], dict)
        assert isinstance(parameters["required"], list)
        assert function["description"].strip()


def test_internal_schema_markers_never_leak_to_the_model() -> None:
    """`column` / `numeric_column` / `min_items` 是我们自己的标记，OpenAI 不认识它们。"""
    text = str(registry.to_openai_tools())
    assert "numeric_column" not in text
    assert "min_items" not in text
    assert "max_items" not in text


def test_schema_translation_keeps_ranges_enums_and_defaults() -> None:
    tools = {tool["function"]["name"]: tool for tool in registry.to_openai_tools()}
    params = tools["group_aggregate"]["function"]["parameters"]

    assert params["properties"]["group_by"]["type"] == "array"
    assert params["properties"]["group_by"]["items"] == {"type": "string"}
    assert params["properties"]["group_by"]["minItems"] == 1
    assert params["properties"]["group_by"]["maxItems"] == 3
    assert params["properties"]["agg"]["enum"] == ["sum", "mean", "count", "min", "max", "median"]
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["limit"]["minimum"] == 1
    assert params["properties"]["limit"]["maximum"] == 100
    assert params["properties"]["sort_order"]["default"] == "desc"
    assert params["required"] == ["group_by", "agg"]


def test_every_tool_has_a_chinese_description_and_a_callable() -> None:
    for spec in registry.list_specs():
        assert spec.description.strip(), spec.name
        assert spec.description.endswith("。"), spec.name
        assert callable(spec.func), spec.name


def test_every_category_is_one_of_the_six_known_ones() -> None:
    for spec in registry.list_specs():
        assert spec.category in CATEGORIES, spec.name


def test_chart_flag_is_set_for_the_tools_that_return_series() -> None:
    for spec in registry.list_specs():
        assert spec.needs_chart is (spec.name in CHART_TOOLS), spec.name


def test_required_params_are_declared_properties() -> None:
    for spec in registry.list_specs():
        properties = spec.params_schema["properties"]
        for name in spec.params_schema["required"]:
            assert name in properties, f"{spec.name} 的必填参数 {name} 没在 properties 里"


def test_enum_rules_always_carry_values() -> None:
    for spec in registry.list_specs():
        for name, rule in spec.params_schema["properties"].items():
            if rule["type"] == "enum":
                assert rule["values"], f"{spec.name}.{name}"


def test_numeric_column_markers_are_on_the_right_params() -> None:
    """数值列的标记必须落在真正要算数的参数上（漏一个就会让文本列混进求和）。"""
    numeric_params = {
        name: {
            param
            for param, rule in spec.params_schema["properties"].items()
            if rule.get("numeric_column")
        }
        for name, spec in ((spec.name, spec) for spec in registry.list_specs())
    }
    assert numeric_params["describe_dataset"] == {"columns"}
    assert numeric_params["group_aggregate"] == {"metric"}
    assert numeric_params["top_n"] == {"by"}
    assert numeric_params["compare_groups"] == {"metric"}
    assert numeric_params["correlation"] == {"columns"}
    assert numeric_params["time_series_summary"] == {"metric"}
    assert numeric_params["missing_values"] == set()


def test_checks_are_callable_or_none() -> None:
    for spec in registry.list_specs():
        assert spec.check is None or callable(spec.check), spec.name


def test_builtin_specs_do_not_depend_on_the_dataframe() -> None:
    """工具定义是**硬编码**的：不扫目录、不按名字自动导入，所以造一份空 df 也能列出全部工具。"""
    empty = pd.DataFrame({"a": [1]})
    spec = registry.get("describe_dataset")
    assert spec is not None
    assert spec.params_schema["type"] == "object"
    assert list(empty.columns) == ["a"]


def test_tool_spec_can_be_built_by_hand() -> None:
    """ToolSpec 是普通的 dataclass，测试可以自己造一个（用来验证 register 的行为）。"""

    def func(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
        return {"row_count": len(df)}

    spec = ToolSpec(
        name="fake_tool",
        description="测试用的假工具。",
        params_schema={"type": "object", "properties": {}, "required": []},
        func=func,
        category="profile",
    )
    assert spec.needs_chart is False
    assert spec.check is None
    assert spec.func(pd.DataFrame({"a": [1, 2]}), {}) == {"row_count": 2}
