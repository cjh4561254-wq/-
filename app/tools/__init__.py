"""工具层：模型能调用的 7 个数据分析工具（白名单 + 校验 + 实现）。

这个包**不在 `__init__.py` 里 import 子模块**，两条理由：

- `app/tools/registry.py` 会 import 4 个工具模块；包初始化时再 import 一遍，等于把
  "任何一处写错"放大成"`import app.tools` 直接失败"，报错位置还更难看懂；
- 包只需要说明自己是什么，用哪一块由调用方显式 import
  （`from app.tools import registry`、`from app.tools.validate import validate_call`）。

四块职责：

- `registry.py`：白名单。哪些工具存在、参数长什么样、说明怎么写（模型的 `tools=` 从这里来）
- `validate.py`：校验 1–4（工具名 / 参数 / 列存在 / 操作合法），按顺序前置短路
- `jsonable.py`：`to_jsonable` 清洗 + `ensure_jsonable` 复核（校验 5）
- `describe.py` / `aggregate.py` / `compare.py` / `timeseries.py`：7 个工具的实现

两条约定：

1. 工具函数签名统一是 `(df, params) -> dict`，返回值必须是"洗过"的纯 Python 类型
   （`to_jsonable` + D4 的 `ensure_jsonable` 都会盯这一点）；
2. 工具**不读文件、不写文件、不调 LLM**：数据由 D2 的 store 交进来，数字由 pandas 算出来，
   "这些数字说明什么"留给 D4 的 LLM 去说。
"""
