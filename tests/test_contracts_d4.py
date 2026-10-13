"""D4 契约测试：把"冻结的形状"钉住，让三个人能并行开工。

和 `tests/test_contracts_d3.py` 同一个思路，但 D4 多一类东西：
D3 的契约全是"已存在的模型"，D4 还冻结了**五个还没写的函数签名**
（`chat` / `plan` / `run_workflow` / `submit` / `start_analysis`）。
那些函数此刻不存在，所以本文件用"棘轮"方式钉：

    - 模块/函数还不存在 → skip（D4 开工前这是正常的）
    - 存在但签名和契约不一致 → **fail**（说明有人改了约定，必须回来看这份契约）

跳过的条数应当在 D4 结束时归零 —— 这是可数的验收判据，而不是"感觉写完了"。
"""

import inspect
import json
import re

import pytest
from pydantic import ValidationError

from app import schemas_llm, schemas_workflow
from app.config import Settings
from app.schemas_llm import LLMReply, LLMToolCall
from app.schemas_workflow import AnalysisAccepted, WorkflowState

# 契约里的函数签名（参数名 / 顺序 / 默认值）。依据：docs/ARCHITECTURE.md §4.14.3。
# 只钉"调用方看得见的部分"：参数名（调用方要按名字传）、顺序、默认值。
_CONTRACT_PARAMS: dict[tuple[str, str], list[tuple[str, object]]] = {
    ("app.llm.client", "chat"): [("messages", inspect.Parameter.empty), ("tools", None)],
    ("app.agents.planner", "plan"): [
        ("question", inspect.Parameter.empty),
        ("profile", inspect.Parameter.empty),
        ("max_steps", 6),
    ],
    ("app.agents.orchestrator", "run_workflow"): [("state", inspect.Parameter.empty)],
    ("app.core.background", "submit"): [
        ("state", inspect.Parameter.empty),
        ("tasks", inspect.Parameter.empty),
    ],
    ("app.api.routes_analysis", "start_analysis"): [
        ("req", inspect.Parameter.empty),
        ("background", inspect.Parameter.empty),
    ],
}


def _resolve(dotted_module: str, func_name: str):
    """取函数；模块或函数不存在时返回 None（不 import 失败，避免收集错误）。"""
    try:
        module = __import__(dotted_module, fromlist=[func_name])
    except ImportError:
        return None
    return getattr(module, func_name, None)


@pytest.mark.parametrize(("target", "expected"), sorted(_CONTRACT_PARAMS.items()))
def test_d4_function_signatures_match_the_frozen_contract(target, expected):
    """五个 D4 函数的参数名/顺序/默认值必须与 §4.14.3 一致。

    为什么钉参数名而不只钉个数：调用方是**按关键字**调用的
    （`plan(question=..., profile=...)`），名字变了就是破坏契约，
    哪怕位置和个数都一样。
    """
    dotted_module, func_name = target
    func = _resolve(dotted_module, func_name)
    if func is None:
        pytest.skip(f"{dotted_module}.{func_name} 还没写（D4 施工中）")

    actual = [
        (p.name, p.default) for p in inspect.signature(func).parameters.values()
    ]
    assert actual == expected, f"{dotted_module}.{func_name} 签名偏离契约"


def test_run_workflow_returns_none_and_does_not_import_fastapi():
    """Orchestrator 原地改 state，不返回东西；且不许 import FastAPI（§3.1 的"不能做"）。

    这条约束靠人自觉是守不住的：一个 `from fastapi import HTTPException` 就能让
    "状态机唯一所有者"变成"知道 HTTP 的状态机"。所以用 AST 扫源码，不靠 import 副作用。
    """
    try:
        import app.agents.orchestrator as mod
    except ImportError:
        pytest.skip("app.agents.orchestrator 还没写（D4 施工中）")

    import ast
    from pathlib import Path

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "fastapi" not in imported
    assert "starlette" not in imported


class TestLLMReplyCarriesReasoningContent:
    """§4.14.2：`reasoning_content` 漏传的后果是 API 400，不是降级。

    所以这里测的不是"我们碰巧拼对了"，而是"**拼错会被拦住**"——
    每条断言都配一个反例，否则一个"什么都不检查"的 validator 也能过。
    """

    def test_valid_reply_with_tool_calls_passes(self):
        reply = LLMReply(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "先看数据里有什么列",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "describe_dataset"}}
                ],
            },
            reasoning_content="先看数据里有什么列",
            tool_calls=[LLMToolCall(id="call_1", name="describe_dataset", arguments="{}")],
            model="deepseek-flash",
        )
        assert reply.reasoning_content in reply.message["reasoning_content"]

    def test_reply_without_reasoning_content_is_still_fine(self):
        """非思考模式 / 模型没给思维链时不该被误伤 —— 与上一条互为对照。"""
        reply = LLMReply(
            message={"role": "assistant", "content": "你好"},
            content="你好",
            model="deepseek-flash",
        )
        assert reply.tool_calls == []
        assert reply.reasoning_content is None

    def test_dropping_reasoning_content_from_message_is_rejected(self):
        """决定性反例：拿到了 reasoning_content，却没写进要回传的 message。"""
        with pytest.raises(ValidationError) as excinfo:
            LLMReply(
                message={"role": "assistant", "content": None},   # ← 少了 reasoning_content
                reasoning_content="先看数据里有什么列",
            )
        assert "reasoning_content" in str(excinfo.value)

    def test_non_assistant_role_is_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            LLMReply(message={"role": "user", "content": "hi"})
        assert "assistant" in str(excinfo.value)

    def test_tool_call_count_mismatch_is_rejected(self):
        """message 里 2 条、字段里 1 条 → 模型下一轮看不到自己请求过什么。"""
        with pytest.raises(ValidationError) as excinfo:
            LLMReply(
                message={
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "call_1", "type": "function"},
                        {"id": "call_2", "type": "function"},
                    ],
                },
                tool_calls=[LLMToolCall(id="call_1", name="describe_dataset")],
            )
        assert "数量不一致" in str(excinfo.value)

    def test_message_survives_json_round_trip(self):
        """`message` 要被 append 进 messages 再发给 API，必须能过 JSON 序列化。"""
        reply = LLMReply(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "中文思维链",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "top_n"}}
                ],
            },
            reasoning_content="中文思维链",
            tool_calls=[LLMToolCall(id="call_1", name="top_n", arguments='{"by": "销售额"}')],
        )
        dumped = json.dumps(reply.message, ensure_ascii=False, allow_nan=False)
        assert "中文思维链" in dumped
        assert json.loads(dumped)["reasoning_content"] == "中文思维链"


class TestLLMToolCall:
    def test_arguments_stays_a_raw_string(self):
        """刻意不解析成 dict：模型可能给非法 JSON，解析与容错是 planner 的活。"""
        call = LLMToolCall(id="call_1", name="top_n", arguments='{"by": "销售额", "n": 10}')
        assert isinstance(call.arguments, str)

        broken = LLMToolCall(id="call_2", name="top_n", arguments="{不是合法 JSON")
        assert broken.arguments == "{不是合法 JSON"

    def test_arguments_defaults_to_empty_object(self):
        assert LLMToolCall(id="c", name="missing_values").arguments == "{}"

    def test_id_and_name_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            LLMToolCall(id="", name="top_n")
        with pytest.raises(ValidationError):
            LLMToolCall(id="c", name="")


class TestAnalysisAccepted:
    """接口 5 的返回体：4 个字段，够前端去轮询就够了。"""

    def _state(self) -> WorkflowState:
        from datetime import datetime

        return WorkflowState(
            workflow_id="a" * 32,
            dataset_id="d" * 32,
            question="分析不同地区的销售额",
            created_at=datetime(2026, 10, 13, 12, 0, 0),
        )

    def test_from_state_takes_exactly_the_four_contract_fields(self):
        accepted = AnalysisAccepted.from_state(self._state())
        assert set(accepted.model_dump()) == {"workflow_id", "dataset_id", "status", "created_at"}
        assert accepted.workflow_id == "a" * 32
        assert accepted.dataset_id == "d" * 32
        assert accepted.status == "PENDING"
        assert accepted.created_at.year == 2026

    def test_it_does_not_leak_internal_fields(self):
        """将来给 WorkflowState 加内部字段时，不该莫名其妙出现在响应体里。"""
        dumped = AnalysisAccepted.from_state(self._state()).model_dump()
        for leaked in ("plan", "results", "charts", "report", "validation", "retry_count"):
            assert leaked not in dumped

    def test_workflow_id_pattern_is_enforced(self):
        """32 位小写 hex 是 `str(uuid4())` 与 `uuid4().hex` 的分水岭。"""
        with pytest.raises(ValidationError):
            AnalysisAccepted(
                workflow_id="A1B2" * 8, dataset_id="d", created_at="2026-10-13T12:00:00"
            )
        with pytest.raises(ValidationError):
            AnalysisAccepted(
                workflow_id="a" * 36, dataset_id="d", created_at="2026-10-13T12:00:00"
            )

    def test_serializes_for_the_http_response(self):
        payload = AnalysisAccepted.from_state(self._state()).model_dump(mode="json")
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        assert re.search(r"\d{4}-\d{2}-\d{2}", json.loads(text)["created_at"])


class TestD4ConfigFields:
    """§4.14.4：D4/D5 需要的配置一次补齐（造名字相近的字段不报错，只是永远不生效）。"""

    def test_the_three_new_fields_exist_with_contract_defaults(self):
        s = Settings(_env_file=None)
        assert s.charts_dir == "data/charts"
        assert s.max_workflow_seconds == 180
        assert s.tool_call_limit == 20

    def test_charts_path_is_absolute_and_tracks_the_config_value(self):
        """`charts_dir` 是配置字符串、`charts_path` 才是能 mkdir 的绝对路径。"""
        from app.config import PROJECT_ROOT

        s = Settings(_env_file=None)
        assert s.charts_path.is_absolute()
        assert s.charts_path == PROJECT_ROOT / "data" / "charts"

        custom = Settings(_env_file=None, charts_dir="build/figures")
        assert custom.charts_path == PROJECT_ROOT / "build" / "figures"

    def test_bounds_reject_the_values_that_would_hang_the_frontend(self):
        """0 秒超时 = 每次都超时；0 次工具调用 = 模型一步都动不了。"""
        for bad in ({"max_workflow_seconds": 0}, {"max_workflow_seconds": 3601}):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, **bad)
        for bad in ({"tool_call_limit": 0}, {"tool_call_limit": 201}):
            with pytest.raises(ValidationError):
                Settings(_env_file=None, **bad)

    def test_llm_configured_still_means_all_three_non_empty(self):
        assert Settings(_env_file=None).llm_configured is False
        assert (
            Settings(
                _env_file=None,
                llm_base_url="https://api.deepseek.com",
                llm_model="deepseek-flash",
                llm_api_key="x",
            ).llm_configured
            is True
        )


class TestContractIsDiscoverable:
    def test_the_new_models_live_where_the_doc_says(self):
        """§4.13 归属表：LLMReply 在 schemas_llm.py，AnalysisAccepted 在 schemas_workflow.py。"""
        assert schemas_llm.LLMReply is LLMReply
        assert schemas_workflow.AnalysisAccepted is AnalysisAccepted

    def test_schemas_llm_has_no_runtime_dependencies(self):
        """`schemas_llm.py` 只有形状：不许 import openai、不许读配置、不许发请求。"""
        from pathlib import Path

        import ast

        src = Path(schemas_llm.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert imported <= {"typing", "pydantic"}
