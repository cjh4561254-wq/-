"""大模型调用层：一次模型调用 → 一个 `LLMReply`。**全项目唯一发 HTTP 给模型的地方。**

这个文件只做三件事：读配置 → 发请求（带重试）→ **手工拼**出 `LLMReply`。
它不 import 任何业务模块（planner / orchestrator / tools 一个都不认识），
所以"模型层"能单独测：`tests/test_llm_client.py` 用 `httpx.MockTransport`
截获真实请求体，不需要网络、不需要密钥。

## 为什么必须手工拼 message，不能把 SDK 对象交给上层（D-18）

DeepSeek 思考模式的官方文档写死了一条：**带 `tools` 的请求，后续每一轮都必须完整
回传 `reasoning_content`，漏了直接 400**（不是降级）。

而 `messages.append(response.choices[0].message)` 之所以"能用"，是因为 openai SDK
把不认识的字段塞进了 `model_extra` 再原样序列化 —— 这是 **SDK 的版本相关行为，
不是 API 契约**。SDK 某次升级收紧它，字段就静默消失，后果是线上 400。

所以这里把每个字段**逐个抄进一个普通 dict**：`role` / `content` / `tool_calls` /
`reasoning_content`。"抄"这个动作写在我们的代码里，就不依赖 SDK 内部机制了。
`LLMReply` 上那个 validator 会再兜一次底：拿到了 `reasoning_content` 却没写进
`message`，构造时就会抛 `ValidationError`。

## 重试策略（架构文档 §5.3 的表格）

| 情况 | 重试吗 | 说明 |
|---|---|---|
| 429 / 500 / 503 | 重试 | 服务端明确说"稍后再来"，等一会儿真的可能成功 |
| 连接错误 / 超时（拿不到状态码） | 重试 | 网络抖动，与"请求本身错"是两回事 |
| 400 / 401 / 402 / 422 | **不重试** | 请求本身有问题：401 密钥错、402 余额不足、400 参数错、422 语义错。再来一次结果一模一样，只是白等 |

间隔固定 `1.5 * attempt` 秒（不做指数退避：这个项目的调用次数少，
"能一眼算出总共等多久"比"理论最优"更有用）。

## 关于"两层重试"的已知代价（写出来，不藏）

`OpenAI(...)` 客户端自己也会重试（SDK 的 `max_retries`），外面这个 for 循环也会重试。
两层叠起来最坏是 `(N+1)²` 次请求，`N=2` 时是 9 次。

仍然这样写的原因：架构文档 §10 安全第 6 条要求把 `settings.llm_max_retries`
**真的传进客户端**（不能留着 SDK 的默认值不管），而 §5.3 又要求退避是
`1.5 * attempt` 并且能统计出 `attempts`（SDK 内部重试我们看不见）。
要收敛成一层，把 `max_retries` 改成 0 即可 —— 那属于契约变更，D4 不做，
已记在回报里交给 Lead 拍板。
"""

import logging
import time
from typing import Any

import openai
from openai import OpenAI

from app import config
from app.schemas_llm import LLMReply, LLMToolCall

logger = logging.getLogger(__name__)

# 只有这三个状态码值得重试。它们是"服务端让你稍后再来"，
# 而不是"你的请求有问题"——后者重试一万次也是同一个结果。
_RETRYABLE_STATUS = frozenset({429, 500, 503})

# 退避基数：第 attempt 次失败后等 1.5 * attempt 秒。
_BACKOFF_BASE_SECONDS = 1.5


class LLMNotConfigured(RuntimeError):
    """没配大模型（三项配置缺任意一项）。

    单独一个异常类型，是为了让调用方能**明确地区分**"模型没配好"和"模型调用失败"：
    前者是**预期内**的常态（第 4 天演示时密钥可能还没配），走规则兜底就行；
    后者才是需要看日志的问题。所以它继承 `RuntimeError` 而不是 `Exception` 家族里
    某个更模糊的类型，调用方 `except LLMNotConfigured` 一眼就知道自己在接什么。
    """


class LLMCallError(RuntimeError):
    """模型调用失败（网络、超时、状态码不可重试、重试次数用尽）。

    带上 `status_code` 与 `attempts` 两个属性：排查时第一个问题是
    "对方返回了什么"，第二个是"我们试了几次"。
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMReply:
    """调用一次模型，返回 `LLMReply`。

    参数：
        messages: 完整的对话历史（含 system）。调用方负责拼历史 ——
                  **上一轮的 `LLMReply.message` 要原样 append 进来**，
                  否则思考模式下会 400（见模块 docstring）。
        tools:    OpenAI 格式的工具列表（由 `registry.to_openai_tools()` 生成）。
                  不传就表示"这次不需要它调工具"（D5 写报告就是这么调的）。

    异常：
        LLMNotConfigured —— 配置不全，调用方应转规则兜底；
        LLMCallError     —— 调用失败（含重试后仍失败）。
    """
    settings = config.get_settings()

    # 配置在**函数内**读，不在模块顶层读：顶层读会把配置绑死在 import 那一刻，
    # 测试里改环境变量、`reset_settings_cache()` 都不再起作用（本项目踩过这个坑）。
    if not settings.llm_configured:
        raise LLMNotConfigured(
            "未配置大模型：base_url 与 api_key 两项都要有值 ——"
            "环境变量 LLM_BASE_URL / LLM_API_KEY（本机已有的 DEEPSEEK_BASE_URL / "
            "DEEPSEEK_API_KEY 同样认）；模型名可用 LLM_MODEL / DEEPSEEK_MODEL 覆盖，"
            "默认 deepseek-flash。"
            "没有它系统会走规则兜底计划，功能不受影响。"
        )

    client = OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        # 超时与重试上限都必须真的传进去（架构文档 §10 安全第 6 条）：
        # 不传的话 SDK 用它的默认值，而"默认值是多少"是会随版本变的。
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )

    # 首次 + 最多 llm_max_retries 次重试。
    total_attempts = settings.llm_max_retries + 1
    started = time.perf_counter()

    for attempt in range(1, total_attempts + 1):
        try:
            response = _create_completion(client, settings.llm_model, messages, tools)
        except openai.APIError as exc:
            # `APIError` 是 SDK 里"调用失败"的公共父类，覆盖 APIStatusError
            # （有 status_code）与 APIConnectionError / APITimeoutError（没有）。
            status = getattr(exc, "status_code", None)
            retryable = status is None or status in _RETRYABLE_STATUS

            if not retryable:
                # 400 / 401 / 402 / 422：当场放弃。日志里只记状态码与类型，
                # 不记异常全文 —— 它可能带上 Authorization 头之类的请求细节。
                logger.error("模型调用被拒绝（HTTP %s），不重试：%s", status, type(exc).__name__)
                raise LLMCallError(
                    f"模型调用失败（HTTP {status}）：{type(exc).__name__}。"
                    "这类错误重试没有意义，请检查密钥、余额或请求参数。",
                    status_code=status,
                    attempts=attempt,
                ) from exc

            if attempt >= total_attempts:
                logger.error("模型调用重试 %s 次仍失败（HTTP %s）", attempt, status)
                raise LLMCallError(
                    f"模型调用失败，已重试 {attempt} 次：{type(exc).__name__}",
                    status_code=status,
                    attempts=attempt,
                ) from exc

            delay = _BACKOFF_BASE_SECONDS * attempt
            logger.warning(
                "模型调用失败（HTTP %s），第 %s 次重试将在 %.1f 秒后发出",
                status,
                attempt,
                delay,
            )
            time.sleep(delay)
            continue

        return _build_reply(response, started, attempt, settings.llm_model)

    # 循环只可能从 return 或 raise 退出，走到这里说明逻辑被改坏了。
    # 留一句明确的报错，比返回 None 让上层莫名其妙崩掉要好。
    raise LLMCallError("模型调用没有产生任何结果（这是 client 内部的逻辑错误）")


def _create_completion(
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> Any:
    """真正发出请求的那一行。单独抽出来是为了让测试能只替换这一层。

    `tools` 为 None 时**不传这个键**，而不是传 `tools=None`：
    OpenAI 的请求体里 `tools: null` 与"没有 tools 字段"是两种不同的请求，
    只有后者才是"这一轮不带工具"。
    """
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    return client.chat.completions.create(**kwargs)


def _build_reply(response: Any, started: float, attempts: int, model: str) -> LLMReply:
    """把 SDK 的 response **逐字段抄**成 `LLMReply`（D-18：不依赖 model_extra 透传）。"""
    choices = getattr(response, "choices", None)
    if not choices:
        # 正常情况下不会发生。真发生了也不能返回一个空 LLMReply 让上层猜，
        # 因为"模型什么都没说"和"请求失败了"对调用方是两件事。
        raise LLMCallError("模型返回里没有 choices，无法解析")

    choice = choices[0]
    sdk_message = choice.message
    reasoning = _read_reasoning_content(sdk_message)
    raw_calls = list(getattr(sdk_message, "tool_calls", None) or [])

    message: dict[str, Any] = {
        "role": "assistant",
        "content": getattr(sdk_message, "content", None),
    }
    if reasoning is not None:
        # 只有真的拿到了才写这个键。写 `"reasoning_content": None` 会把
        # "非思考模式"伪装成"思考模式但思维链为空"，两种情况的后续处理不一样。
        message["reasoning_content"] = reasoning

    tool_calls: list[LLMToolCall] = []
    if raw_calls:
        # 手工拼成 OpenAI 原生形状（含 type 与 function 子对象）：
        # 这个 dict 会被上层原样 append 回 messages 再发出去，形状必须是 API 认的。
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in raw_calls
        ]
        # `LLMToolCall` 是给上层用的**扁平视图**（id/name/arguments），
        # 与 message 里的原生形状是同一份数据的两种表达，条数必须一致
        # ——`LLMReply` 的 validator 会检查这一点。
        tool_calls = [
            LLMToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for call in raw_calls
        ]

    usage = getattr(response, "usage", None)

    return LLMReply(
        message=message,
        content=getattr(sdk_message, "content", None),
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        model=getattr(response, "model", None) or model,
        finish_reason=getattr(choice, "finish_reason", None),
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        elapsed_ms=int(round((time.perf_counter() - started) * 1000)),
        attempts=attempts,
    )


def _read_reasoning_content(sdk_message: Any) -> str | None:
    """从 SDK 的 message 上取 `reasoning_content`（思考模式的思维链）。

    两条路径都试，是因为它不属于 OpenAI 官方字段：
    - `getattr(message, "reasoning_content")` —— pydantic 允许直接访问 extra 字段；
    - `message.model_extra["reasoning_content"]` —— 有些 SDK 版本只放进这里。

    哪个版本走哪条路是 SDK 的实现细节，我们两条都兜住；
    取到空字符串也当作"没有"（模型给了个空的思维链，等同于没给）。
    """
    value = getattr(sdk_message, "reasoning_content", None)
    if value is None:
        extra = getattr(sdk_message, "model_extra", None) or {}
        value = extra.get("reasoning_content")
    if isinstance(value, str) and value:
        return value
    return None
