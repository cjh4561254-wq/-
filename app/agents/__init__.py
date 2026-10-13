"""智能体层：把"用户的问题"变成"真实的计算"，中间**没有一行 LLM 参与执行**。

这个包 **不在 `__init__.py` 里 import 子模块**，理由和 `app/tools/__init__.py` 一样：
包初始化时顺手 import 一遍，等于把"任何一处写错"放大成 `import app.agents` 直接失败，
而且报错位置更难看懂。包只说明自己是什么，用哪一块由调用方显式 import：

    from app.agents.analysis import run_task
    from app.agents.planner import plan_with_rules

两块职责（架构文档 §3.2 / §3.4）：
- `planner.py`：把中文问题变成一份**可执行的任务计划**（D3 只有关键词规则兜底，LLM 路径 D4 加）。
- `analysis.py`：执行计划里的**单个**任务，调 `app/tools/` 里的白名单工具真算，返回 `ToolResult`。
"""
