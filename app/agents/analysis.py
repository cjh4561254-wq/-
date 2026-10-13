"""数据分析智能体：执行**单个**分析任务，返回结构化的 `ToolResult`。

## 这个文件只有一件事，但它是整条链路的地基

用户问"各地区销售额怎么样" → planner 给出计划 → **这里**把计划里的每个任务
真的算出来。执行阶段一行 LLM 调用都没有：模型只负责"选哪个工具、填什么参数"，
数字全部由 pandas 算（架构文档 §3.3）。所以这个文件里
**一行 `df.groupby` 都不许有**——分析逻辑全在 `app/tools/` 里，
这里只做"校验 → 调用 → 清洗"三件事。

## 为什么 `run_task` 永远不抛异常

它是"任务执行器"。一个任务失败（比如模型给的列名不存在）不该让整条工作流崩掉：
Orchestrator 需要看到"第 1、2 个任务成功了、第 3 个失败了"，再决定是重试、
跳过还是继续（架构文档 §5.3 / §5.4）。异常一旦逃出去，这些信息就全丢了——
用户看到的会是一个 500，而不是"这一步没算出来，其他结果仍然给你"。

代价是：**所有失败都必须变成 `ok=False` 的 `ToolResult`**，而不是被吞掉。
所以每个兜底分支都要 `logger.exception(...)` 打一行，把原始 traceback 留在日志里——
D4 的 Orchestrator 靠 `error_code` 决定要不要重试，而排查时只有 `error_message`
是远远不够的（"ValueError" 三个字说明不了是哪一行）。
"""

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from app.schemas_tool import ToolResult
from app.schemas_workflow import AnalysisTask
from app.tools.jsonable import ensure_jsonable
from app.tools.validate import ToolValidationError, validate_call

logger = logging.getLogger(__name__)

# 工具实现里"能预料到"的异常。这类异常意味着工具函数自己有问题（对文本列求均值、
# 拿不存在的键去取字典……），属于开发期 bug；归到 BAD_RESULT，和"模型给错参数"区分开。
_TOOL_BUG_ERRORS = (KeyError, TypeError, ValueError)


def run_task(task: AnalysisTask, df: pd.DataFrame) -> ToolResult:
    """执行一个分析任务，返回结构化的 `ToolResult`。**永远不抛异常。**

    五类校验按顺序前置短路（架构文档 §6.3）：1–4 在 `validate_call` 里，
    第 5 类（返回值能不能进 JSON）在最后单独做。

    参数：
        task: 要执行的任务。**只读**，函数不会改它（改了会影响计划本身，
              而计划是"这次分析的依据"，D5 生成报告时还要回头引用）。
        df:   数据集。由调用方（D4 的 Orchestrator）从 store 里取好传进来——
              这个模块**不碰文件、不碰数据库、不碰网络**，纯函数最好测。
    """
    started = time.perf_counter()

    try:
        # 校验 1–4：工具名存在 → 参数合法 → 列存在 → 操作合法。
        # 任何一条不过都抛 ToolValidationError，下面统一接住。
        # 注意这里是**再查一次注册表**（validate_call 内部调 registry.get），
        # 绝不用 getattr(module, task.tool_name) 或 eval——那等于给模型开了执行任意代码的口子。
        spec = validate_call(task.tool_name, task.params, df)

        # 真正的计算。工具函数签名统一是 (df, params) -> dict
        raw_result = spec.func(df, task.params)

        # 校验 5：返回值必须是"能进 JSON 的纯 Python 类型"。
        # 不过则抛 ToolValidationError("BAD_RESULT")：NaN、numpy 标量这类东西
        # json.dumps 时才会炸，或者前端解析出一个 NaN —— 那时离出错的地方已经很远了。
        data = ensure_jsonable(raw_result)

        if not isinstance(data, dict):
            # 工具函数返回了 list / str 之类。Pydantic 的 ToolResult.data 是 dict，
            # 直接塞进去会抛 ValidationError——而这个函数不许抛，所以在这里就拦下来，
            # 并给一句能直接定位到工具的话（而不是让人去看 Pydantic 的报错原文）。
            return _failure(
                task,
                started,
                "BAD_RESULT",
                f"工具 {task.tool_name} 必须返回 dict，实际返回了 {type(raw_result).__name__}",
            )

        return ToolResult(
            task_id=task.task_id,
            tool_name=task.tool_name,
            ok=True,
            data=data,
            columns=_as_columns(data.get("columns")),
            row_count=_as_row_count(data.get("row_count")),
            elapsed_ms=_elapsed_ms(started),
            created_at=datetime.now(timezone.utc),
        )

    except ToolValidationError as exc:
        # 校验 1–5 拒绝。这是**预期内**的失败（模型给错参数、列名不存在），
        # 不需要 traceback：错误消息已经写清楚是哪一步、哪个参数错了。
        return _failure(task, started, exc.error_code, exc.error_message)

    except _TOOL_BUG_ERRORS as exc:
        # 工具实现自己出的问题。打完整堆栈：这种是开发期 bug，必须能一眼看到哪一行。
        logger.exception("工具 %s 执行任务 %s 时出错", task.tool_name, task.task_id)
        return _failure(
            task,
            started,
            "BAD_RESULT",
            f"工具 {task.tool_name} 执行时出错（{type(exc).__name__}）：{exc}",
        )

    except Exception as exc:  # noqa: BLE001 —— 这里**故意**兜住所有异常，见模块 docstring
        # 兜底。走到这里说明有我们没预料到的情况（例如 pandas 抛了它自己的异常类型）。
        # 归到 BAD_RESULT（"返回结构/执行过程不符合预期"），并把异常类型名放进消息：
        # D4 的 Orchestrator 会按 error_code 决定重试策略，类型名是它判断的唯一线索。
        logger.exception("工具 %s 执行任务 %s 时出现未预期的异常", task.tool_name, task.task_id)
        return _failure(
            task,
            started,
            "BAD_RESULT",
            f"工具 {task.tool_name} 执行时出现未预期的错误（{type(exc).__name__}）：{exc}",
        )


def _failure(task: AnalysisTask, started: float, error_code: str, error_message: str) -> ToolResult:
    """把失败组装成 `ok=False` 的 `ToolResult`（成功与失败返回**同一个形状**）。

    为什么要统一形状：调用方（Orchestrator、前端）不该写"成功走这条路、失败走那条路"
    两套解析逻辑。失败时 `data` 是空字典而不是 `None`，前端可以直接 `data.get(...)`
    而不用先判空。

    `tool_name` 用 `task.tool_name` 而不是"真的跑的那个"——这里压根没跑到工具；
    记下用户/模型**想调**哪个工具，才能看出"模型是不是老报一个不存在的工具名"。
    """
    return ToolResult(
        task_id=task.task_id,
        tool_name=task.tool_name,
        ok=False,
        elapsed_ms=_elapsed_ms(started),
        error_code=error_code,  # type: ignore[arg-type]  # 只可能是 5 个合法码之一
        error_message=error_message,
        created_at=datetime.now(timezone.utc),
    )


def _elapsed_ms(started: float) -> int:
    """从 `time.perf_counter()` 的起点算到现在的毫秒数。

    用 `perf_counter` 而不是 `time.time()`：后者是"墙上时钟"，会被系统对时
    （NTP 校正）往回拨，算出负数耗时。`perf_counter` 只保证单调递增。

    结果取整成 `int`：`ToolResult.elapsed_ms` 是 `int`（`ge=0`），
    传浮点会被 Pydantic 直接拒绝。毫秒级的时间统计不需要小数位。
    """
    return int(round((time.perf_counter() - started) * 1000))


def _as_columns(value: object) -> list[str]:
    """把工具返回的 `columns` 收成 `list[str]`；没给就给空列表。

    两处"多余"的处理，都是为了守住"永远不抛异常"：
    - 工具的列名可能来自 pandas 的 `Index`，元素不一定是 `str`，而 `ToolResult.columns`
      声明的是 `list[str]`——混进一个非字符串同样会让 Pydantic 抛 `ValidationError`。
    - `data` 里根本没有 `columns` 键（工具没返回）是**正常情况**，给 `[]` 就好。

    但"给了却不是列表"是工具写错了，所以要 `logger.warning` 留一条痕迹：
    默默吞掉的话，前端拿到的列名列表永远是空的，而没有任何地方提示过原因。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("工具返回的 columns 不是列表（%s），已按空列表处理", type(value).__name__)
        return []
    return [str(name) for name in value]


def _as_row_count(value: object) -> int:
    """把工具返回的 `row_count` 收成非负整数；没给就给 `0`。

    `ToolResult.row_count` 是 `int` 且有 `ge=0` 约束，而"这个函数永远不抛异常"是硬要求。
    如果工具返回了 `-1` 或 `2.5`，直接塞给 Pydantic 就会抛 `ValidationError` 逃出去——
    把"工具算错了"升级成"整条工作流崩了"，代价完全不成比例。所以这里挡一道。

    挡下来之后**必须留一条 warning**：`row_count` 错了往往意味着工具里
    `int(len(...))` 写漏了，静默改成 0 会让"返回 0 行"看起来像数据本身的问题。

    `bool` 要单独排掉：Python 里 `True` 也是 `int`（值为 1），
    但"行数是 True"显然不是我们想要的。
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logger.warning("工具返回的 row_count 不是非负整数（%r），已按 0 处理", value)
        return 0
    return value
