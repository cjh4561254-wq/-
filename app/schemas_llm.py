"""模型请求层的返回形状：`LLMReply` / `LLMToolCall`。

为什么单独开一个文件，而不是塞进 `schemas_workflow.py`
------------------------------------------------------
架构文档 §4.0 的判据是"数据长什么样"进 `schemas_*.py`，`schemas_workflow.py`
回答的是"一条工作流长什么样"。而这里的两个模型回答的是"**一次模型调用返回了什么**"，
它既不属于工作流、也不属于工具，是 `app/llm/` 这个包的边界类型，所以自己一个文件。
逐文件清单见 §4.13。

这个文件只有 Pydantic 模型，**零业务逻辑**：不 import openai、不读配置、不发请求。

`reasoning_content` 为什么要在这里被"结构性地"保证（本文件最重要的设计）
----------------------------------------------------------------------
DeepSeek 官方文档（思考模式）写死了一条：

    携带了 `tools` 参数的请求，在后续所有请求中，必须完整回传
    `reasoning_content` 给 API —— 即使该轮模型未实际进行工具调用。
    若您的代码中未正确回传 `reasoning_content`，API 会返回 400 报错。

注意后果不是"降级"而是**直接 400**。而 openai SDK 的
`response.choices[0].message` 之所以能被直接 `messages.append(...)` 回传，
是因为 SDK 把未知字段塞进了 `model_extra` 并原样序列化 —— 这是
**SDK 的版本相关行为，不是 API 契约**。换个 SDK 版本就可能静默丢掉它，
而丢掉的后果是线上 400。

所以本文件把"要回传什么"变成一个**字段**：`LLMReply.message` 就是
"下一个请求里要原样 append 的那条 assistant 消息"（一个普通 dict）。
`client.chat()` 负责**手工拼**这个 dict，而不是把 SDK 对象丢给上层。
上面那条 `@model_validator` 则保证"既然拿到了 `reasoning_content`，
`message` 里就必须有它" —— 这个约定因此不可能被某次改动悄悄破坏。

（实测记录：openai 2.41.0 下两条路径实际都会把 `reasoning_content` 发出去，
见 §14.7。仍然手工拼，理由就是"不依赖 SDK 内部机制"。）
"""

from typing import Any

from pydantic import BaseModel, Field, model_validator


class LLMToolCall(BaseModel):
    """模型要求调用的一次工具调用。

    `arguments` 是**模型给的原始 JSON 字符串**，这里刻意不解析成 dict：
    - 模型可能给出非法 JSON（架构文档 §13 风险 1 就列了这一条），
      解析与三层容错是 `planner.py` 的职责，不是"数据形状"的职责；
    - 保留原文才能在重试时把"你的 JSON 不合法：<原文>"附回 Prompt。
    """

    id: str = Field(min_length=1, description="模型给的调用 id，回填 tool 消息时必须原样带上")
    name: str = Field(min_length=1, description="模型想调用的工具名（可能是白名单外的）")
    arguments: str = Field(default="{}", description="原始 JSON 字符串，未经解析")


class LLMReply(BaseModel):
    """`app/llm/client.py` 的 `chat()` 返回值：一次模型调用的全部结果。

    它是 `app/llm/` 的对外边界类型，所以字段都做成"上层真正要用的东西"，
    而不是把 SDK 的 response 对象整个透传出去（那样上层就得懂 openai SDK，
    换 SDK 时全项目都要改）。
    """

    message: dict[str, Any] = Field(
        description=(
            "**下一个请求里要原样 append 的那条 assistant 消息**（含 role/content/"
            "reasoning_content/tool_calls）。由 client 手工拼出，不依赖 SDK 的 model_extra。"
        )
    )
    content: str | None = Field(default=None, description="模型的自然语言答复；只调工具时可能为空")
    reasoning_content: str | None = Field(
        default=None,
        description="思考模式的思维链。带 tools 的后续请求必须回传，否则 API 返回 400",
    )
    tool_calls: list[LLMToolCall] = Field(
        default_factory=list, description="模型要求调用的工具；为空表示它直接给了最终答复"
    )

    model: str = Field(default="", description="实际服务的模型名（回显，便于日志核对）")
    finish_reason: str | None = Field(default=None, description="如 stop / tool_calls / length")
    prompt_tokens: int | None = Field(default=None, ge=0, description="用量；服务未返回时为 None")
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: int = Field(default=0, ge=0, description="本次调用耗时（含重试）")
    attempts: int = Field(default=1, ge=1, description="实际发出的 HTTP 尝试次数，>1 说明重试过")

    @model_validator(mode="after")
    def _check_reasoning_content_is_carried(self) -> "LLMReply":
        """把"必须回传 reasoning_content"从文档约定变成构造时就拦得住的约束。

        检查三件事：
        ① `message` 必须是 assistant 消息（回填 messages 时 role 写错会得到 400）；
        ② 拿到了 `reasoning_content`，`message` 里就必须有它（漏掉 = 400）；
        ③ 有 tool_calls 时，`message["tool_calls"]` 条数必须对得上
           （少了任何一条，模型下一轮就看不到自己请求过什么）。

        为什么放在模型里而不是写进 client 的注释：注释拦不住改动，
        而这个校验在**任何**构造 LLMReply 的地方都会跑（包括将来新增的调用点）。
        """
        if self.message.get("role") != "assistant":
            raise ValueError("message 必须是 role='assistant' 的消息，否则回填 messages 会 400")

        if self.reasoning_content is not None and "reasoning_content" not in self.message:
            raise ValueError(
                "拿到了 reasoning_content 但 message 里没有它 —— "
                "带 tools 的后续请求必须完整回传，否则 API 返回 400"
            )

        if self.tool_calls:
            raw_calls = self.message.get("tool_calls")
            if not isinstance(raw_calls, list) or len(raw_calls) != len(self.tool_calls):
                raise ValueError(
                    f"tool_calls 数量不一致：message 里 {raw_calls!r}，字段里 {len(self.tool_calls)} 条"
                )

        return self
