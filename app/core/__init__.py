"""核心层：跨 Agent 共享的**进程内状态**。

这个包 **不在 `__init__.py` 里 import 子模块**，理由和 `app/tools/__init__.py` 一样：
包初始化时顺手 import 一遍，等于把"任何一处写错"放大成 `import app.core` 直接失败，
而且报错位置更难看懂。包只说明自己是什么，用哪一块由调用方显式 import：

    from app.core import registry

目前只有一块职责：
- `registry.py`：工作流状态注册表（进程内 `dict` + 一把锁，架构文档 §8.2.1）
"""
