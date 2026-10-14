"""`app/llm/client.py` 的测试：重试策略、message 手工拼装、`reasoning_content` 回传。

为什么这个文件值得单独写
------------------------
`client.chat()` 是全项目**唯一**会真的花钱、真的超时的地方，而它最关键的两条行为
都是"看不见"的：

1. **重试策略**：429/500/503 要重试，400/401/402/422 **一次都不能重试**。
   写错了不会报错 —— 只是白等 1.5 + 3 秒，或者拿同一个错误再撞三次墙。
   所以断言必须落在**请求次数**和**睡了多久**上。
2. **`reasoning_content` 必须出现在第二个请求体里**（`docs/DECISIONS.md` D-18）：
   漏了它 API 直接 400（不是降级）。而 openai SDK 恰好会"顺手"把它带上
   （未知字段落进 `model_extra` 再原样序列化），所以**只看 SDK 对象上有没有这个属性
   是测不出问题的** —— 必须截获**真实发出去的 JSON**。

本文件用 `httpx.MockTransport` 做到后一件事：它替换掉传输层，请求仍然会被真的组装、
真的被序列化，只是不出网。断言的是"发出去的字节"，不是"我们以为发了什么"。

三条纪律
--------
- **绝不发真实网络请求**：所有用例都换掉传输层；`tests/` 里不该有任何一条会出网的用例。
- **不依赖本机环境**：配置全部由夹具 `monkeypatch.setenv` 显式给定；本机真实存在的
  `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` 由 conftest.py 的 `_isolate_llm_env` 清掉。
  否则这些用例会真的拿用户的密钥发请求，或者"未配置"断言在用户机器上直接变红。
- **不依赖 SDK 自己的重试**：夹具构造客户端时把 SDK 的 `max_retries` 钉成 0，
  这样"请求了几次"完全由 `chat()` 里那个循环决定，数出来的次数才有意义。
"""

import json
from typing import Any

import httpx
import pytest
from openai import OpenAI

from app import config
from app.llm import client as client_module
from app.llm.client import LLMCallError, LLMNotConfigured, chat

# 假的配置值。它们只活在环境变量里，不会离开本进程（传输层被换成了 MockTransport）。
FAKE_BASE_URL = "https://api.deepseek.com"
FAKE_MODEL = "deepseek-chat"
FAKE_API_KEY = "test-key-not-real"

# 一条最小但形状完整的 chat.completion 响应体。
# `reasoning_content` 不在 OpenAI 官方字段里，它是 DeepSeek 思考模式加的 ——
# 所以它必须放在 message 里"平级"出现，不能塞进某个嵌套对象。
FAKE_TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "top_n", "arguments": '{"by": "销售额", "n": 3}'},
}


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch):
    """每个用例都给一套**显式**的配置，并在结束后清掉缓存。

    `reset_settings_cache()` 这行不能省：`get_settings()` 带 `lru_cache`，
    不清缓存的话下一个用例读到的还是上一个用例的配置（本项目真踩过这个坑）。
    """
    monkeypatch.setenv("LLM_BASE_URL", FAKE_BASE_URL)
    monkeypatch.setenv("LLM_MODEL", FAKE_MODEL)
    monkeypatch.setenv("LLM_API_KEY", FAKE_API_KEY)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()


def _completion(**message_fields: Any) -> dict[str, Any]:
    """造一个响应体，`message_fields` 会合进 `choices[0].message`。"""
    message: dict[str, Any] = {"role": "assistant", "content": ""}
    message.update(message_fields)
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": FAKE_MODEL,
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _install_transport(monkeypatch, handler) -> dict[str, Any]:
    """把 `client` 模块里的 `OpenAI` 换成一个"走 MockTransport 的真客户端"。

    为什么换 `OpenAI` 而不是换 `chat`：换掉 `chat` 就等于把要测的东西整个跳过去了。
    换掉客户端之后，`chat()` 里从"读配置 → 建客户端 → 发请求 → 解析响应"这整条路
    都真的跑了一遍，只有最后一跳（TCP）被替换成内存里的 handler。

    返回它收到的构造参数，供断言"配置真的传进去了"。

    注意 `max_retries=0`：这是**故意**覆盖掉我们传进去的那个值 —— SDK 自己也会重试，
    两层叠起来就数不清"到底是谁重试的"。这里让 SDK 闭嘴，请求次数才等于我们循环的次数。
    """
    seen: dict[str, Any] = {}

    def factory(**kwargs: Any) -> OpenAI:
        seen.update(kwargs)
        return OpenAI(
            api_key=kwargs["api_key"],
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=0,
        )

    monkeypatch.setattr(client_module, "OpenAI", factory)
    return seen


def _freeze_sleep(monkeypatch) -> list[float]:
    """把 `time.sleep` 换成记账的假函数，返回它记下的每一段等待。

    不这样做的话，重试用例会真的睡 1.5 + 3 秒 —— 测试从 0.1 秒变成 5 秒，
    而且"睡多久"这件事反而没人断言了。
    """
    slept: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: slept.append(seconds))
    return slept


def _body(request: httpx.Request) -> dict[str, Any]:
    """取出真实发出去的那个请求体。"""
    return json.loads(request.content.decode("utf-8"))


# ---------------------------------------------------------------------------
# 没配模型：必须在发请求之前就抛，而不是先发一次再失败
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"])
def test_not_configured_raises_without_any_request(monkeypatch, missing: str):
    """三项配置缺任意一项 → `LLMNotConfigured`，而且**一次请求都没发**。

    断言"没发请求"很重要：如果实现是先建客户端、发了再报错，用户就会看到
    "明明没配密钥却打了一串网络错误日志"，而真正的原因（没配）被埋在下面。
    """
    monkeypatch.setenv(missing, "")
    config.reset_settings_cache()

    sent: list[Any] = []
    _install_transport(monkeypatch, lambda request: sent.append(request))

    with pytest.raises(LLMNotConfigured) as excinfo:
        chat([{"role": "user", "content": "你好"}])

    assert sent == []
    message = str(excinfo.value)
    # 报错要说清缺哪几个键，否则用户只能去翻源码
    for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        assert key in message


# ---------------------------------------------------------------------------
# 请求体形状：配置真的传进去了吗、tools 该不该出现
# ---------------------------------------------------------------------------


def test_timeout_is_passed_and_the_sdk_retries_are_pinned_to_zero(monkeypatch):
    """`timeout` 必须真的传给 `OpenAI()`；`max_retries` 必须是**写死的 0**（D-25）。

    两件事性质不同，所以断言也不同：

    - `timeout` 来自配置，不传的话 SDK 用它的默认值，而"默认值是多少"是随版本变的
      （架构文档 §10 安全第 6 条要求把它真的传进去）。
    - `max_retries` **故意**不来自配置：SDK 的重试与 `chat()` 里那个循环是叠乘的，
      传 2 会让单次调用最坏 9 次 HTTP、约 9 分钟（见 `app/llm/client.py` 顶部 §重试）。
      所以它必须是常量 0。这里写死断言 0，正是为了防住"好心把它改回配置值"。
    """
    seen = _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion()))
    chat([{"role": "user", "content": "你好"}])

    assert seen["timeout"] == 60
    assert seen["max_retries"] == 0
    assert seen["base_url"] == FAKE_BASE_URL
    assert seen["api_key"] == FAKE_API_KEY


def test_config_values_are_read_not_hardcoded(monkeypatch):
    """换个环境变量，传进去的值就得跟着变 —— 否则上面那条断言只是在给写死的常量鼓掌。"""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    config.reset_settings_cache()

    seen = _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion()))
    chat([{"role": "user", "content": "你好"}])

    assert seen["timeout"] == 7
    # `llm_max_retries` 现在**只**决定外层循环的次数，不再传给 SDK —— 它已经被
    # 「只发一次请求」的用例单独钉住（test_sdk_max_retries_stays_zero_when_the_config_asks_for_more）。
    assert seen["max_retries"] == 0


def test_sdk_max_retries_stays_zero_when_the_config_asks_for_more(monkeypatch):
    """配置把重试调到 5，SDK 那层仍必须是 0 —— 叠乘就是这样被挡住的。

    这条是 D-25 的核心断言：只要有人把 `max_retries=0` 改回 `settings.llm_max_retries`，
    这里立刻红（同时 `test_config_values_are_read_not_hardcoded` 也会红）。
    """
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    config.reset_settings_cache()

    seen = _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion()))
    chat([{"role": "user", "content": "你好"}])

    assert seen["max_retries"] == 0


def test_model_comes_from_settings(monkeypatch):
    bodies: list[dict[str, Any]] = []
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(200, json=_completion()))[1],
    )
    chat([{"role": "user", "content": "你好"}])
    assert bodies[0]["model"] == FAKE_MODEL


def test_tools_key_is_absent_when_no_tools_given(monkeypatch):
    """不传 `tools` 时，请求体里**不能出现 `tools: null`**。

    `tools: null` 与"没有 tools 字段"是两种不同的请求：前者在思考模式下
    可能被服务端当成"这一轮带工具但列表是空的"，与"这轮不需要工具"不是一回事。
    """
    bodies: list[dict[str, Any]] = []
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(200, json=_completion()))[1],
    )
    chat([{"role": "user", "content": "你好"}])
    assert "tools" not in bodies[0]


def test_tools_are_forwarded_verbatim(monkeypatch):
    tools = [{"type": "function", "function": {"name": "top_n", "parameters": {"type": "object"}}}]
    bodies: list[dict[str, Any]] = []
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(200, json=_completion()))[1],
    )
    chat([{"role": "user", "content": "你好"}], tools=tools)
    assert bodies[0]["tools"] == tools


def test_messages_are_forwarded_verbatim(monkeypatch):
    """用户给的 messages 原样发出，客户端不许自己增删（拼历史是上层的事）。"""
    messages = [
        {"role": "system", "content": "你是数据分析助手"},
        {"role": "user", "content": "看看数据"},
    ]
    bodies: list[dict[str, Any]] = []
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(200, json=_completion()))[1],
    )
    chat(messages)
    assert bodies[0]["messages"] == messages


# ---------------------------------------------------------------------------
# LLMReply 的拼装：手工抄字段，不透传 SDK 对象
# ---------------------------------------------------------------------------


def test_reply_is_assembled_by_hand_with_native_tool_call_shape(monkeypatch):
    """`message` 必须是**普通 dict**，且 `tool_calls` 是 OpenAI 原生形状。

    原生形状指的是每项都有 `type: "function"` 和一个 `function` 子对象 ——
    因为这个 dict 会被上层原样 append 回 `messages` 再发出去，形状必须是 API 认的。
    而 `LLMToolCall` 是给上层看的**扁平视图**（id/name/arguments），是同一份数据的另一种表达。
    """
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_completion(reasoning_content="想一下", tool_calls=[FAKE_TOOL_CALL])),
    )
    reply = chat([{"role": "user", "content": "看看数据"}], tools=[{"type": "function", "function": {"name": "top_n"}}])

    assert isinstance(reply.message, dict)
    assert reply.message["role"] == "assistant"
    assert reply.message["tool_calls"] == [FAKE_TOOL_CALL]

    assert [call.name for call in reply.tool_calls] == ["top_n"]
    assert reply.tool_calls[0].arguments == '{"by": "销售额", "n": 3}'
    assert reply.tool_calls[0].id == "call_1"

    assert reply.reasoning_content == "想一下"
    assert reply.message["reasoning_content"] == "想一下"


def test_reply_carries_model_tokens_and_finish_reason(monkeypatch):
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion(content="好了")))
    reply = chat([{"role": "user", "content": "你好"}])

    assert reply.content == "好了"
    assert reply.model == FAKE_MODEL
    assert reply.finish_reason == "stop"
    assert (reply.prompt_tokens, reply.completion_tokens, reply.total_tokens) == (11, 7, 18)


def test_elapsed_ms_is_a_non_negative_int(monkeypatch):
    """`elapsed_ms` 契约上是 `int`（`Field(ge=0)`）—— 传浮点会被 Pydantic 当场拒绝。"""
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion()))
    reply = chat([{"role": "user", "content": "你好"}])

    assert isinstance(reply.elapsed_ms, int)
    assert reply.elapsed_ms >= 0
    assert reply.attempts == 1


def test_reasoning_key_is_absent_when_model_did_not_think(monkeypatch):
    """非思考模式（没有 `reasoning_content`）时**不写这个键**。

    写成 `"reasoning_content": None` 会把"非思考模式"伪装成"思考模式但思维链为空"，
    而这两种情况在回填历史时的处理并不一样。
    """
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion(content="好了")))
    reply = chat([{"role": "user", "content": "你好"}])

    assert reply.reasoning_content is None
    assert "reasoning_content" not in reply.message


def test_empty_reasoning_is_treated_as_absent(monkeypatch):
    """模型给了个空字符串的思维链，等同于没给 —— 空串进请求体毫无意义。"""
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=_completion(reasoning_content="")))
    reply = chat([{"role": "user", "content": "你好"}])

    assert reply.reasoning_content is None
    assert "reasoning_content" not in reply.message


def test_message_survives_a_json_round_trip(monkeypatch):
    """`message` 要能真的序列化再发出去 —— 中文不能变成转义，也不能有 NaN。"""
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_completion(reasoning_content="中文思维链", tool_calls=[FAKE_TOOL_CALL])),
    )
    reply = chat([{"role": "user", "content": "看看数据"}])

    dumped = json.dumps(reply.message, ensure_ascii=False, allow_nan=False)
    assert "中文思维链" in dumped
    assert json.loads(dumped)["reasoning_content"] == "中文思维链"


# ---------------------------------------------------------------------------
# 最重要的一条：reasoning_content 必须真的出现在第二个请求体里
# ---------------------------------------------------------------------------


def test_reasoning_content_is_really_sent_back_in_the_second_request(monkeypatch):
    """两轮对话：第二轮请求体里必须带着第一轮的 `reasoning_content`。

    这条断言的对象是**实际发出去的 JSON**，不是 SDK 对象上的属性 ——
    因为 SDK 恰好也会带上它（走 `model_extra`），只查属性的话，
    就算我们代码里那行漏了，测试照样绿（D-18 记录的就是这个陷阱）。

    漏了它的后果是 API 直接 400：思考模式下带 `tools` 的后续请求必须完整回传。
    """
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_body(request))
        return httpx.Response(
            200,
            json=_completion(reasoning_content="中文思维链", tool_calls=[FAKE_TOOL_CALL]),
        )

    _install_transport(monkeypatch, handler)
    tools = [{"type": "function", "function": {"name": "top_n"}}]

    # 第一轮：模型给出工具调用 + 思维链
    messages: list[dict[str, Any]] = [{"role": "user", "content": "看看数据"}]
    first = chat(messages, tools=tools)

    # 第二轮：把上一轮那条 assistant 消息**原样** append 回去（契约 §4.14.2 的写法）
    messages.append(first.message)
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": "{}"})
    chat(messages, tools=tools)

    assert len(bodies) == 2
    second_turn = bodies[1]["messages"]
    assert [item["role"] for item in second_turn] == ["user", "assistant", "tool"]

    assistant_turn = second_turn[1]
    assert assistant_turn["reasoning_content"] == "中文思维链"
    # 原生形状也要原样带回去：只回传思维链、丢了 tool_calls 一样会 400
    assert assistant_turn["tool_calls"] == [FAKE_TOOL_CALL]


def test_second_request_has_no_reasoning_key_when_there_was_none(monkeypatch):
    """反过来也要成立：第一轮没有思维链，第二轮就不该凭空多出一个空键。"""
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_body(request))
        return httpx.Response(200, json=_completion(content="好了"))

    _install_transport(monkeypatch, handler)

    messages: list[dict[str, Any]] = [{"role": "user", "content": "你好"}]
    first = chat(messages)
    messages.append(first.message)
    messages.append({"role": "user", "content": "再说一遍"})
    chat(messages)

    assert "reasoning_content" not in bodies[1]["messages"][1]


# ---------------------------------------------------------------------------
# 重试策略：只有 429/500/503 值得重试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_is_retried_once_then_succeeds(monkeypatch, status: int):
    """第一次 429/500/503 → 等 1.5 秒重试 → 第二次成功。

    断言三件事：请求了 2 次、睡的是 `1.5 * 1`、`attempts` 如实记成 2。
    """
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_body(request))
        if len(bodies) == 1:
            return httpx.Response(status, json={"error": {"message": "稍后再试"}})
        return httpx.Response(200, json=_completion(content="好了"))

    slept = _freeze_sleep(monkeypatch)
    _install_transport(monkeypatch, handler)

    reply = chat([{"role": "user", "content": "你好"}])

    assert len(bodies) == 2
    assert slept == [1.5]
    assert reply.attempts == 2
    assert reply.content == "好了"


def test_retry_backoff_grows_with_each_attempt(monkeypatch):
    """退避是 `1.5 * attempt`：连续三次 503 → 睡 1.5 秒、再睡 3.0 秒。"""
    slept = _freeze_sleep(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(503, json={"error": {"message": "服务不可用"}}),
    )

    with pytest.raises(LLMCallError) as excinfo:
        chat([{"role": "user", "content": "你好"}])

    assert slept == [1.5, 3.0]
    assert excinfo.value.attempts == 3
    assert excinfo.value.status_code == 503


def test_retry_count_comes_from_settings(monkeypatch):
    """`llm_max_retries` 改成 0 → 一次都不重试（总请求数 = 1）。"""
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    config.reset_settings_cache()

    bodies: list[dict[str, Any]] = []
    slept = _freeze_sleep(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(503, json={"error": {}}))[1],
    )

    with pytest.raises(LLMCallError) as excinfo:
        chat([{"role": "user", "content": "你好"}])

    assert len(bodies) == 1
    assert slept == []
    assert excinfo.value.attempts == 1


@pytest.mark.parametrize("status", [400, 401, 402, 422])
def test_non_retryable_status_is_not_retried(monkeypatch, status: int):
    """400/401/402/422 一次都不重试，而且**一次都不睡**。

    这四种是"请求本身有问题"：401 密钥错、402 余额不足、400 参数错、422 语义错。
    再来一次结果一模一样，只是让用户多等 4.5 秒。
    """
    bodies: list[dict[str, Any]] = []
    slept = _freeze_sleep(monkeypatch)
    _install_transport(
        monkeypatch,
        lambda request: (bodies.append(_body(request)), httpx.Response(status, json={"error": {"message": "不行"}}))[1],
    )

    with pytest.raises(LLMCallError) as excinfo:
        chat([{"role": "user", "content": "你好"}])

    assert len(bodies) == 1
    assert slept == []
    assert excinfo.value.status_code == status
    assert excinfo.value.attempts == 1
    # 报错要能让人知道"重试没用"，否则排查的人第一反应是"是不是网络抖了"
    assert "重试没有意义" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 兜底：拿不到 choices 时不能返回一个空 reply 让上层猜
# ---------------------------------------------------------------------------


def test_missing_choices_raises_call_error(monkeypatch):
    """响应里没有 choices → `LLMCallError`。

    "模型什么都没说"和"请求失败了"对调用方是两件事，不能返回一个空 `LLMReply`
    让上层自己去猜 —— 上层猜的结果通常是"那就按没有工具调用来处理"，于是
    一次本该报错的调用变成了一次静默的空计划。
    """
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"id": "x", "object": "chat.completion", "created": 1, "model": FAKE_MODEL, "choices": []},
        ),
    )

    with pytest.raises(LLMCallError) as excinfo:
        chat([{"role": "user", "content": "你好"}])

    assert "choices" in str(excinfo.value)
