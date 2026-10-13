"""把 pandas / numpy 算出来的值洗成"纯 JSON 类型"，并在出口复核一遍。

这里有两个职责不同的函数，**不要合并**：

- `to_jsonable(value)` 是"翻译器"：工具函数返回结果前用它，把 numpy 标量、NaN、
  Timestamp 换成 JSON 原生类型。
- `ensure_jsonable(data)` 是"裁判"：D4 收尾时用它，**不信工具已经清洗过**，自己再走
  一遍树确认只剩 JSON 原生类型，最后跑一次 `json.dumps(data, allow_nan=False)`。

只留翻译器：某个工具忘了调用，numpy 标量会静默漏到接口层，前端报 500 而看不出是谁干的。
只留裁判：每个工具都得自己写一遍转换，迟早有人写漏一种类型。
两个都在，"忘了清洗的工具"会在 D4 被 `BAD_RESULT` 当场抓住。

为什么必须洗（真实踩过的坑）
----------------------------
| 算出来是什么 | `json.dumps` 的结果 |
|---|---|
| `np.int64(3)` | `TypeError: Object of type int64 is not JSON serializable` |
| `np.bool_(True)` | `TypeError: Object of type bool is not JSON serializable` |
| `np.float64(1.5)` | 能过（它是 `float` 的子类） |
| `nan` / `inf` | **`ValueError: Out of range float values are not JSON compliant: nan`** |
| `pd.Timestamp` / `pd.NaT` / `pd.NA` | `TypeError` |

- `np.int64` **不是** `int` 的子类（`isinstance(np.int64(3), int)` → `False`），
  所以"看着是整数"的值会让序列化直接失败；
- `nan` 更阴：它不是类型错误，而是"值不在 JSON 规范里"。漏一个 NaN 出去，
  请求会在**那一条数据**上炸，其它数据全正常 —— 这种 bug 最难查。

翻译规则（顺序有讲究）
----------------------
1. `None` / `pd.NA` / `pd.NaT` → `None`
2. `dict` / `list` / `tuple` → 递归清洗
3. `bool` / `np.bool_` → `bool`（**必须排在整数之前**：Python 里 `True == 1`，
   先走整数分支会把 `True` 变成 `1`）
4. 整数（含 `np.int64` 等）→ `int`
5. 浮点（含 `np.float64`）→ 有限值 `float`；`nan` / `inf` / `-inf` → `None`
6. `str` → 原样
7. `pd.Timestamp` / `datetime` / `date` / `np.datetime64` → ISO 字符串
8. `pd.Series` / `pd.Index` / `np.ndarray` → 列表（递归）
9. 其它类型 → `str(value)` 兜底（宁可变成字符串，也不要一路走到 `json.dumps` 才炸：
   前端看到一个字符串还能显示，看到一个 500 只能重试）
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd


def to_jsonable(value: Any) -> Any:
    """把任意 pandas/numpy 值转成 JSON 能直接序列化的纯 Python 值。"""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        # 键统一转字符串：分组结果的键有可能是 numpy 整数
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        # JSON 里没有 NaN / Infinity，统一变成 null 让前端显示成"—"
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        return value
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (pd.Series, pd.Index, np.ndarray)):
        return [to_jsonable(item) for item in value.tolist()]
    return str(value)


def ensure_jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """复核 `data` 里只剩 JSON 原生类型，再跑一次 `json.dumps`；不改内容，返回原对象。

    通过 → 原样返回；不通过 → `ToolValidationError("BAD_RESULT", ...)`，
    消息里带出错的位置（例如 `$.rows[3].销售额`），方便直接定位是哪个工具漏了清洗。

    为什么判断类型要用 `type(x) is` 而不是 `isinstance(x, float)`：
    `isinstance(np.float64(1.0), float)` 是 `True`（numpy 标量是 Python 标量的子类），
    用 isinstance 会把 numpy 标量放过去 —— 那这个裁判就白设了。
    """
    # 函数内 import：validate.py 会 import registry，registry 又要 import 各工具模块，
    # 而工具模块要 import 本模块 —— 在模块顶部 import 会成环。
    from app.tools.validate import ToolValidationError

    _walk_jsonable(data, "$", ToolValidationError)
    try:
        json.dumps(data, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # 上面的树检查通常已经拦住了，这里只是兜底
        raise ToolValidationError(
            "BAD_RESULT",
            f"结果无法序列化成 JSON：{exc}",
            detail="请先用 to_jsonable() 清洗工具返回值",
        ) from exc
    return data


def _walk_jsonable(value: Any, path: str, error: type[Exception]) -> None:
    """递归检查一棵树里有没有 JSON 放不下的东西。`path` 用来报位置。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise error(
                    "BAD_RESULT",
                    f"结果的键必须是字符串：{path} 里的键 {key!r} 是 {type(key).__name__}",
                    detail="dict 的键请统一转成 str（to_jsonable 会做）",
                )
            _walk_jsonable(item, f"{path}.{key}", error)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_jsonable(item, f"{path}[{index}]", error)
        return
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise error(
                "BAD_RESULT",
                f"结果是 NaN 或无穷大，JSON 里放不下：{path}",
                detail="请用 to_jsonable() 把 nan/inf 变成 None",
            )
        return
    raise error(
        "BAD_RESULT",
        f"结果里有 JSON 放不下的类型：{path} 是 {type(value).__name__}（值 {value!r}）",
        detail="请先用 to_jsonable() 清洗工具返回值",
    )
