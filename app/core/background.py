"""后台执行器：把一条工作流丢进 Starlette 的线程池，并保证它"无论怎么死"都有收尾。

这个文件为什么存在（架构文档 §8.2 零件 2、§5.4 第 4 条）
------------------------------------------------------
`POST /api/analysis` 只做四件事：校验数据集存在、把状态登记进 registry、
把执行任务登记到 `BackgroundTasks`、立刻返回 202。真正跑分析的是这里。

"受理"与"跑完"分开之后，就多出一个必须自己兜住的风险：
**后台线程如果静默死掉，前端会永远转圈** —— 它只知道 `status` 一直不是终态。
所以 `_run_safely` 存在的唯一目的，是把"线程死了"这件事翻译成一条
`status=FAILED` 的状态：前端看到它就是"出错了"，而不是"还在跑"。

为什么捕获 `BaseException` 而不是 `Exception`（这不是笔误）
--------------------------------------------------------
`KeyboardInterrupt` / `SystemExit` / `GeneratorExit` 都是 `BaseException` 的直接
子类，**不继承 `Exception`**。只写 `except Exception` 的话它们会穿透 `try`：
`finally` 仍会执行（Python 语义如此），但异常会把线程带走，`state.status` 永远
停在 `PROFILING`、`state.error` 是空的 —— 正是上面那个"永远转圈"。
宁可把一个 Ctrl+C 记成"分析失败"，也不能让前端无限等。

为什么不给 `state.xxx = ...` 加锁（架构文档 §8.2.1 规则 1 明令禁止）
---------------------------------------------------------------
① 后台**只有一个线程**在顺序推进同一条工作流，不存在两个线程同时写同一个 state；
② CPython 里单个属性赋值是原子的，前端最坏也只是"慢一拍"；
③ 一旦把"改 state 字段"也纳入 registry 那把锁，"持锁 → 改 state → 触发回调 →
   又要拿锁"就会自己等自己，死锁只是时间问题。
需要锁保护的只有 `registry._workflows` 那个 dict —— 那边已经保护好了。
"""

from datetime import datetime, timezone

from fastapi import BackgroundTasks

from app.agents.orchestrator import run_workflow
from app.errors import ErrorCode
from app.schemas import ApiError
from app.schemas_workflow import WorkflowState, WorkflowStatus


def submit(state: WorkflowState, tasks: BackgroundTasks) -> None:
    """把这条工作流交给 Starlette 的后台任务队列。**就这一行，不要包装。**

    为什么用 `BackgroundTasks` 而不是自己 `threading.Thread`：
        ① 注入进来的 `BackgroundTasks` 由框架在**响应发出去之后**执行，天然满足
           "先返回 202、再去跑分析"；
        ② `run_workflow` 是同步函数，Starlette 会把它丢进线程池，不占事件循环；
        ③ 自己起线程就要自己管生命周期（异常往哪报、进程退出时怎么办），
           而这些框架已经处理好了。多写一层 = 多一处会写错的地方（架构文档 §8.2）。
    """
    tasks.add_task(_run_safely, state)


def _run_safely(state: WorkflowState) -> None:
    """跑 `run_workflow`，并保证"无论成功、失败还是被强杀"都有收尾。

    三层结构各自负责一件事，缺一不可：
        `try`     —— 记下真正开始的时间（前端拿它算"排队等了多久"）；
        `except`  —— 把任何逃出来的异常（含 `BaseException`）落成 `status=FAILED`；
        `finally` —— **无论走哪条路**都盖上"结束时间 + step=done"，让前端一定能
                     从"还在跑"里走出来。`finished_at` 是前端的唯一完成信号之一。

    为什么 `detail` 要截断到 500 字：它会进响应体，而响应体会被截图、被贴进
    issue。完整堆栈留在服务端日志里（`app/main.py` 的兜底 handler 也是这么做的）。
    """
    try:
        state.started_at = datetime.now(timezone.utc)
        run_workflow(state)  # Orchestrator，同步函数；Starlette 会把它丢进线程池
    except BaseException as exc:  # 兜底：绝不让线程静默死掉（理由见模块 docstring）
        state.status = WorkflowStatus.FAILED
        state.error = ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="分析过程中出现未预期错误",
            detail=str(exc)[:500],
        )
    finally:
        state.finished_at = datetime.now(timezone.utc)
        state.step = "done"
