"""Prompt 模板：把"中文问题 + 数据画像"拼成模型看得懂的 `messages`。

为什么单独一个文件，而不是写在 `planner.py` 里
--------------------------------------------
① **改动频率完全不同**：调 prompt 是"改几句话试试效果"，改解析逻辑是"改代码结构"。
   混在一起的话，每次调 prompt 都要在一堆 JSON 容错代码里翻找字符串。
② **可以单独测**：本文件是纯函数（进字符串、出 list/dict），测试可以直接断言
   "画像文本里真的带上了真实列名"，不需要启动 planner、不需要 mock 网络。

写 prompt 的三条硬规矩（架构文档 §3 第 5 条：LLM 不能做的事）
-----------------------------------------------------------
1. **只能引用给出的列名**：列名清单由 `profile_text()` 现场生成，模型没有别的来源。
2. **不许自己算数字**：它只负责"选哪个工具、填什么参数"，算数是 pandas 的事。
3. **不许输出代码**：所以 system prompt 里明确写了"不要写代码、不要写 SQL"。

这三条不是"建议"，是项目的信任边界：报告里的每个数字都要能追溯到工具结果
（D5 的 `validate()` 就是靠这一点抓编造）。
"""

from typing import Any

from app.schemas_dataset import DatasetProfile

# 系统提示词。写成一个模块级常量而不是函数里的字符串：
# 它是"给模型看的接口"，会被反复对照着改，放在文件顶部一眼能看到全貌。
PLANNER_SYSTEM_PROMPT = """你是一个数据分析助手，负责把用户的中文问题翻译成**工具调用**。

你只能做一件事：从系统提供的工具列表里挑选合适的工具，并填好参数。
绝对不要自己做任何计算，绝对不要输出 Python 代码或 SQL。

参数里的列名只能来自下面这份数据画像里列出的真实列名：
- 画像里没有的列名，一律不许出现在参数里；
- 如果不确定该用哪一列，就选语义最接近的那一列；
- 一个问题可以调用多个工具（最多 6 个），也可以只调用一个。

不要解释、不要总结、不要输出 JSON 文本 —— 直接调用工具。"""

# 工具参数不是合法 JSON 时，附回给模型的提示词。
# 注意"把原文也贴回去"：只说"你的 JSON 不合法"，模型无从知道错在哪；
# 贴回原文它才能看到自己写了什么（架构文档 §5.3 的重试规则就是这么定的）。
_RETRY_NOTE_TEMPLATE = """你上一次给出的工具参数不是合法 JSON，无法解析。

你写的是：
{raw}

请重新调用工具，`arguments` 必须是一个合法的 JSON 对象（键用双引号，不要加注释，
不要用单引号，不要在 JSON 前后加解释文字）。"""


def build_planner_messages(
    question: str, profile: DatasetProfile, retry_note: str | None = None
) -> list[dict[str, Any]]:
    """拼出第一次请求的 `messages`。

    `retry_note` 不为 None 时会**再追加一条 user 消息**，而不是改掉原来那条：
    模型看到的完整历史是"我问了什么 → 它答错了 → 用户指出错在哪"，
    这样它才知道要改的是上一轮的回答。直接把原文改掉反而会让它重新规划一遍。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"用户的问题：{question}\n\n数据画像：\n{profile_text(profile)}",
        },
    ]
    if retry_note:
        messages.append({"role": "user", "content": retry_note})
    return messages


def build_retry_note(raw_arguments: str) -> str:
    """把模型上一轮写坏的参数原文包成一条提示词。"""
    return _RETRY_NOTE_TEMPLATE.format(raw=raw_arguments)


def profile_text(profile: DatasetProfile) -> str:
    """把画像压成一段给模型看的文字。

    为什么不用 `profile.model_dump_json()` 直接扔过去
    ----------------------------------------------
    那个 JSON 里有 `mean_value` / `q1_value` / `top_values` 等一堆统计量，
    序列化出来好几千字，其中大部分和"选哪一列"无关，还容易让模型把
    **统计数字**当成可以引用的结论（那就变成"模型自己算数"了）。
    这里只给三样它真正需要的东西：**列名、语义类型、缺失情况**。

    缺失情况是刻意保留的：模型看到"销售额 缺失 18 个"时，
    更可能选 `missing_values` 而不是硬算平均值。
    """
    lines = [f"数据集共 {profile.row_count} 行、{profile.column_count} 列。列如下："]

    for column in profile.columns:
        # 语义类型用中文括注，比 "categorical" 这种词对模型更友好
        lines.append(
            f"- {column.name}（{_SEMANTIC_TYPE_LABELS.get(column.semantic_type, column.semantic_type)}，"
            f"缺失 {column.null_count} 个，{column.unique_count} 个不同值）"
        )

    # 再单独列一次数值列：模型选 `metric` / `by` 这类参数时最常需要它，
    # 让它自己从上面 9 行里挑出"哪几个是数值"属于没必要的负担，也更容易挑错。
    numeric = [c.name for c in profile.columns if c.semantic_type == "numeric"]
    if numeric:
        lines.append(f"\n可以参与计算的数值列：{'、'.join(numeric)}")
    else:
        lines.append("\n这份数据里没有数值列，做不了求和、均值这类计算。")

    if profile.duplicate_row_count:
        lines.append(f"数据里有 {profile.duplicate_row_count} 行完全重复。")

    return "\n".join(lines)


# 语义类型 → 中文说明。用 dict 而不是 if/elif：将来加类型时只多一行。
_SEMANTIC_TYPE_LABELS: dict[str, str] = {
    "numeric": "数值",
    "categorical": "分类",
    "datetime": "日期",
    "boolean": "布尔",
    "text": "文本",
}
