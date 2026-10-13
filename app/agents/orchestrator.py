"""工作流编排器：按 6 个阶段推进一条工作流。**全项目唯一有权改 `state` 的地方。**

## 为什么"唯一所有权"不是洁癖

`state` 会被后台线程写、被前端线程读（轮询接口 6）。如果每个 Agent 都能随手改它，
"谁在什么时候把 status 改成了这个值"就没有答案了 —— 而 `status` 是前端**唯一**的
完成信号（`finished_at` / `has_report` 都是它的附属）。所以架构文档 §3 第 1 条把写权限
收拢到这里：别的 Agent 收普通参数、返回**新对象**。

由此推出一条实现纪律（§8.2.1 规则 1）：**改 state 的字段一律不加锁**。
理由是三点合在一起：① 后台只有这一个线程在顺序推进同一条工作流；
② CPython 里单个属性赋值是原子的，前端最坏只是"慢一拍"；
③ 一旦把"改字段"也纳入 `core/registry` 那把锁，"持锁 → 改 state → 触发回调 →
又要拿锁"就是自己等自己。那把锁只保护它自己的 dict。

## 为什么不 import fastapi（有一条 AST 测试盯着）

状态机一旦认识 `HTTPException`，很快就会有人在这里写 `raise HTTPException(400)`，
"受理"与"执行"的边界就没了。执行失败的正确表达是 `state.error = ApiError(...)`，
至于 HTTP 层把它翻译成什么状态码，是 `app/main.py` 的事（§7.0 那张表）。

## 为什么自己不调 LLM

只调 `planner.plan()` 与 `validation.build_report()`。模型调用集中在两处的好处是
"哪里会花钱、哪里会超时"一眼可数；这里再散一次，§5.3 的超时与重试规则就管不住了。
"""

import logging
from datetime import datetime, timezone

from app import config
from app.agents.analysis import run_task
from app.agents.planner import plan as plan_tasks
from app.agents.validation import build_report
from app.data.profiler import build_profile
from app.data.store import store
from app.errors import AppError
from app.schemas import ApiError, ErrorCode
from app.schemas_workflow import TaskStatus, WorkflowState, WorkflowStatus

logger = logging.getLogger(__name__)

# 6 个阶段的顺序。`step_index` 就是"已完成的阶段数"，所以它的值 = 当前阶段的下标。
# 写成元组而不是散落的字面量：前端进度条按这个顺序画，改顺序必须改一处。
_STEPS = ("profiling", "planning", "execution", "charting", "reporting", "validation")

# 整条工作流的重试总次数上限（与 WorkflowState.retry_count 的 le=6 对齐）。
_MAX_WORKFLOW_RETRIES = 6

# 一个任务最多额外重试几次。§5.3 的规定是"TypeError/ValueError 重试 1 次且换策略"，
# 而"换策略"落在本实现里就是**重试后仍然失败就跳过这个任务**（不阻断其他任务）。
_MAX_TASK_RETRIES = 1


def run_workflow(state: WorkflowState) -> None:
    """把一条工作流从头推到终态。**原地改 `state`，不返回东西。**

    调用方是 `app/core/background.py` 的 `_run_safely`：它负责写 `started_at`、
    兜住任何逃出来的异常、并在 `finally` 里盖 `finished_at`。这里再写一次
    `finished_at` / `step="done"` 是有意的**重复**：正常路径上它们本来就该是
    Orchestrator 写的（§5.1 结尾），调用方那次只是"被强杀时也要有个收尾"的保险。
    """
    settings = config.get_settings()

    # 数据集在进程内存里（§8.2.3：重启就没了）。取不到就没法往下走，
    # 而且这是一个**明确的**失败原因，不该让后面的代码在 None 上崩。
    df = store.get_frame(state.dataset_id)
    if df is None:
        _fail(
            state,
            ErrorCode.NOT_FOUND,
            "数据集不在内存里，无法分析",
            detail="数据只存在服务进程的内存中，服务重启后需要重新上传。",
        )
        return

    # ---- 阶段 1/6：profiling ----
    _enter(state, WorkflowStatus.PROFILING, "profiling")
    try:
        state.profile = _resolve_profile(state, df)
    except Exception as exc:  # 画像失败 = 后面所有阶段都没有依据，只能收工
        logger.exception("工作流 %s 生成画像失败", state.workflow_id)
        _fail(
            state,
            ErrorCode.INTERNAL_ERROR,
            "生成数据画像时出错",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return

    # ---- 阶段 2/6：planning ----
    if _stop_reason(state, settings) is not None:
        return
    _enter(state, WorkflowStatus.PLANNING, "planning")
    try:
        # plan() 的契约是"恒不抛异常、永远返回可执行计划"，所以这里不需要兜底分支。
        # 仍然包一层 try：契约被破坏时宁可写一条 FAILED，也不能让异常逃出去
        # 把工作流卡在 PLANNING（前端会一直转圈）。
        state.plan = plan_tasks(state.question, state.profile, max_steps=state.max_steps)
    except Exception as exc:
        logger.exception("工作流 %s 制定计划失败", state.workflow_id)
        _fail(
            state,
            ErrorCode.INTERNAL_ERROR,
            "制定分析计划时出错",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return

    # ---- 阶段 3/6：execution ----
    _enter(state, WorkflowStatus.EXECUTING, "execution")
    stop = _run_tasks(state, df, settings)
    if stop is not None:
        return

    # ---- 阶段 4/6：charting ----
    if _stop_reason(state, settings) is not None:
        return
    _enter(state, WorkflowStatus.CHARTING, "charting")
    _render_charts(state)

    # ---- 阶段 5/6：reporting ----
    _enter(state, WorkflowStatus.REPORTING, "reporting")
    try:
        state.report = build_report(state)
        state.has_report = state.report is not None
    except Exception as exc:
        # build_report 内部自己兜了一层，正常到不了这里。真到了也不能让工作流
        # 变成 FAILED：任务其实都算完了，用户该看到"结果有了、报告没生成"。
        logger.exception("工作流 %s 生成报告失败", state.workflow_id)
        state.has_report = False
        state.error = ApiError(
            code=ErrorCode.INTERNAL_ERROR,
            message="分析已完成，但生成报告时出错",
            detail=f"{type(exc).__name__}: {exc}",
        )

    # ---- 阶段 6/6：validation ----
    _enter(state, WorkflowStatus.VALIDATING, "validation")
    # D5 才做 `validate()`（报告里的数字逐个溯源，抓编造）。D4 在这里只做一件事：
    # 依据"有没有失败的任务 / 有没有出报告"决定最终状态（§5.4 第 5 条）。
    # `state.validation` 保持 None 是**诚实**的：没跑过的校验不该给一个空壳结果。
    _finish(state, _final_status(state))


def _resolve_profile(state: WorkflowState, df: object) -> object:
    """取画像：先看 state、再看 store 缓存、最后现算一份。

    三级顺序的理由：`state.profile` 可能是重跑时带进来的；store 里的那份是
    上传后就算好的（不重算，省一次逐列统计）；都没有才自己算，并**顺手缓存**，
    这样"先提问再去看画像接口"不会算两遍。
    """
    profile = state.profile or store.get_profile(state.dataset_id)
    if profile is not None:
        return profile

    profile = build_profile(df, state.dataset_id)
    try:
        store.set_profile(state.dataset_id, profile)
    except AppError:
        # 数据集没登记过（测试里直接造一个 state 就会这样）。缓存不上完全不影响
        # 这次分析的结果，所以不打断流程，只记一条 debug。
        logger.debug("数据集 %s 未登记，画像未写入缓存", state.dataset_id)
    return profile


def _run_tasks(state: WorkflowState, df: object, settings: object) -> str | None:
    """逐个执行计划里的任务。返回 `None` 表示跑完，否则返回中断原因。

    单个任务失败**不中断**工作流（§5.4 第 1 条）：用户宁可看到"3 个结果 + 1 个失败"，
    也不要因为一个列名写错就什么都拿不到。
    """
    assert state.plan is not None  # 调用点保证：planning 阶段失败会提前 return

    for task in state.plan.tasks:
        reason = _stop_reason(state, settings)
        if reason is not None:
            return reason

        if _dependencies_failed(task, state.plan.tasks):
            # `depends_on` 里有失败的任务：这个任务的输入已经不可信了，跳过它。
            # 注意 SKIPPED **只**影响显式声明了依赖的任务，不搞"一个失败全体连坐"。
            task.status = TaskStatus.SKIPPED
            task.error = "依赖的任务失败了，已跳过"
            continue

        _execute_one(state, task, df)

    return None


def _execute_one(state: WorkflowState, task: object, df: object) -> None:
    """跑一个任务，必要时重试一次，最后把状态写进 `task`。

    重试规则（§5.3 那张表）在本实现里的落点：
    - `BAD_PARAMS` / `COLUMN_NOT_FOUND` / `TOOL_NOT_FOUND` / `BAD_OPERATION`：
      **不重试**。参数是模型给的，重试同一个参数只会得到同一个结果。
    - `BAD_RESULT`（工具自己抛了 KeyError/TypeError/ValueError，或返回值进不了 JSON）：
      重试 1 次。这种是"实现有问题"的信号，重试大概率还是失败 ——
      所以重试之后仍然失败就**跳过它继续跑后面的任务**，这才是"换策略"。
    """
    task.status = TaskStatus.RUNNING

    result = run_task(task, df)
    state.results.append(result)
    state.tool_call_count += 1

    if result.ok:
        task.status = TaskStatus.SUCCESS
        task.error = None
        return

    if (
        result.error_code == "BAD_RESULT"
        and task.attempt < _MAX_TASK_RETRIES
        and state.retry_count < _MAX_WORKFLOW_RETRIES
    ):
        task.status = TaskStatus.RETRYING
        task.attempt += 1
        state.retry_count += 1
        state.heartbeat_at = _now()  # 重试也要刷心跳，否则会被判超时

        retry = run_task(task, df)
        state.results.append(retry)  # 两次尝试都留痕：排查时"第一次报了什么"很重要
        state.tool_call_count += 1

        if retry.ok:
            task.status = TaskStatus.SUCCESS
            task.error = None
            return
        result = retry

    task.status = TaskStatus.FAILED
    task.error = result.error_message or "任务执行失败"


def _render_charts(state: WorkflowState) -> None:
    """D4 的 charting 阶段：**明确地什么都不做**，并把理由写进日志。

    两样东西都还没到：`app/agents/charts.py` 是 D5 的文件；D4 的规则计划器也不会给任务
    配 `AnalysisTask.chart`（规则兜底只产出工具调用）。这种情况下唯一正确的做法是
    让 `state.charts` 保持空列表。

    **绝不伪造 `ChartResult`**：前端拿到一个指向不存在 PNG 的 URL，比"这次没有图"
    难排查得多 —— 前者看起来像后端坏了，后者只是"还没做"。

    D5 的落点（写在这里，免得接手的人去翻文档）：遍历 `state.plan.tasks`，
    对 `task.chart is not None` 且对应 `ToolResult` 成功的任务调
    `charts.render_chart(...)`；出图失败记 `MISSING_CHART_FILE` warning 并继续
    （§5.4 第 2 条），**不要登记半个图表对象**（§8.2.2）。
    """
    logger.info("D4 阶段不出图（charts 模块与 chart 请求都要到 D5 才有），state.charts 保持为空")


def _final_status(state: WorkflowState) -> WorkflowStatus:
    """最终状态：全绿才是 `SUCCESS`，有任何可说的东西就 `SUCCESS_WITH_WARNINGS`。

    三种"有话说"：① 有任务失败；② 有任务被跳过（依赖失败）；③ 报告没出来
    （`state.error` 记着原因）。§5.4 第 5 条只写了第一种，另外两种是同一个道理的延伸 ——
    状态里说 `SUCCESS`，报告里却写着"有 2 项没跑出来"，那是自相矛盾。
    """
    if state.report is None or state.error is not None:
        return WorkflowStatus.SUCCESS_WITH_WARNINGS
    if any(not result.ok for result in state.results):
        return WorkflowStatus.SUCCESS_WITH_WARNINGS
    if state.plan is not None and any(
        task.status is not TaskStatus.SUCCESS for task in state.plan.tasks
    ):
        return WorkflowStatus.SUCCESS_WITH_WARNINGS
    return WorkflowStatus.SUCCESS


def _stop_reason(state: WorkflowState, settings: object) -> str | None:
    """检查两条"整条工作流级别的上限"，超了就落 FAILED 并返回原因。

    为什么这两条必须卡在**每个阶段之前 + 每个任务之前**：它们是防"模型 + 重试"
    组合成死循环的最后一道闸（§5.3）。只在开头查一次的话，一个 20 次工具调用的
    计划照样能跑满；只在结尾查一次的话，用户要多等几分钟才看到失败。
    """
    if _timed_out(state, settings):
        _fail(
            state,
            ErrorCode.WORKFLOW_TIMEOUT,
            "分析超时",
            detail=f"超过 {settings.max_workflow_seconds} 秒仍未完成，已中止。",
        )
        return "timeout"

    if state.tool_call_count >= settings.tool_call_limit:
        # 没有专门的错误码：这是"我们自己没兜住的失控"，归 INTERNAL_ERROR 最诚实。
        # 用 WORKFLOW_TIMEOUT 会误导成"只是慢"。
        _fail(
            state,
            ErrorCode.INTERNAL_ERROR,
            "工具调用次数超过上限，已中止",
            detail=f"已调用 {state.tool_call_count} 次，上限 {settings.tool_call_limit} 次。",
        )
        return "tool_call_limit"

    return None


def _timed_out(state: WorkflowState, settings: object) -> bool:
    """工作流是不是跑太久了。

    起点优先用 `started_at`（后台线程真正开跑的时间），没有就退回 `created_at`
    （"排队等了多久"也算进总时长，这对用户更公平）。

    朴素时间要补上 UTC 时区：`WorkflowState.created_at` 是普通 `datetime`，
    测试里手写 `datetime(2026, 10, 13)` 是很自然的写法，而**朴素时间减带时区时间
    会直接抛 TypeError** —— 那会把"超时判断"变成"工作流崩了"，所以这里补一次。
    """
    started = state.started_at or state.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (_now() - started).total_seconds() > settings.max_workflow_seconds


def _dependencies_failed(task: object, tasks: list) -> bool:
    """这个任务依赖的任务里，有没有失败的（或被跳过的）。

    被跳过的也算失败：依赖链上只要有一环没跑出结果，下游拿到的输入就是不完整的。
    """
    if not task.depends_on:
        return False
    by_id = {other.task_id: other for other in tasks}
    for dependency in task.depends_on:
        other = by_id.get(dependency)
        if other is not None and other.status in (TaskStatus.FAILED, TaskStatus.SKIPPED):
            return True
    return False


def _enter(state: WorkflowState, status: WorkflowStatus, step: str) -> None:
    """进入一个阶段：写状态 + 刷心跳。

    `step_index` 用"当前阶段在 `_STEPS` 里的下标"，正好等于**已完成的阶段数**，
    前端拿它除以 `total_steps` 就是进度条。
    """
    state.status = status
    state.step = step  # type: ignore[assignment]  # step 只能是 _STEPS 里的值
    state.step_index = _STEPS.index(step)
    state.heartbeat_at = _now()


def _finish(state: WorkflowState, status: WorkflowStatus) -> None:
    """到达终态：写 `finished_at`、最后一次心跳、`step="done"`（§5.1 结尾的要求）。"""
    now = _now()
    state.status = status
    state.step = "done"
    state.step_index = len(_STEPS)  # 6/6，前端进度条走满
    state.heartbeat_at = now
    state.finished_at = now


def _fail(state: WorkflowState, code: ErrorCode, message: str, detail: str | None = None) -> None:
    """落一条 FAILED。

    这里**不动 `step_index`**：它表示"已完成几个阶段"，失败时把进度条拉满
    会让人以为整条流程跑完了。`step` 置成 `done` 是另一回事 —— 那是"别再等它了"。
    """
    now = _now()
    state.status = WorkflowStatus.FAILED
    state.error = ApiError(code=code, message=message, detail=detail)
    state.step = "done"
    state.heartbeat_at = now
    state.finished_at = now


def _now() -> datetime:
    """统一的时间口径：**带时区的 UTC**。

    全项目都用它（`created_at` / `heartbeat_at` / `finished_at` 全是 UTC）。
    不要用不带时区的 `datetime.now()`：轮询接口要拿 `heartbeat_at` 和当前时间
    做减法判超时，一个带时区一个不带就会抛 TypeError。
    """
    return datetime.now(timezone.utc)
