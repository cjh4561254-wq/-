"""大模型调用包：把"和 OpenAI 兼容服务说话"这件事关在这一层里。

为什么不在这里 import 子模块
---------------------------
和 `app/tools/__init__.py`、`app/core/__init__.py` 同一个策略：包初始化时不去 import
任何子模块。理由很实际 —— 如果 `__init__.py` 里写了 `from app.llm import client`，
那么"client.py 里有一个语法错误 / openai 没装好"会放大成
**`import app.llm` 直接失败**，连不碰模型的规则兜底路径都一起挂掉。

所以调用方显式写 `from app.llm import client` 或 `from app.llm.client import chat`，
出了问题是哪一个文件的问题，一眼就能看出来。
"""
