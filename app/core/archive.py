"""历史归档门面：把到达终态的工作流写进 SQLite 历史库（§14 决策 3 / §8.3.1）。

## 归档是**旁路**，不是第二份事实来源

§8.3.1 写死了一条纪律：**内存态 `registry._workflows` 仍然是唯一事实来源**，
SQLite 只负责"终态之后留一条记录"。所以这个模块只有两个方向：

    写 —— `archive_if_terminal(state)`：终态时把 `state` 抄成一条 `HistoryRecord`；
    读 —— 历史接口直接读库，**不回头去内存里捞**。

"库里没有就去内存里找一条补上"这种 fallback 是被明令禁止的（见
`app/api/routes_workflows.py` 的两个历史接口）：那会让"重启后历史还在"这个
唯一的卖点变成"有时候在、有时候不在"，而且两条路径的字段来源不同，
迟早出现同一个 `workflow_id` 两个版本。

## 为什么这里的异常必须被吞掉

归档失败（磁盘满、目录只读、库文件被占用）**不能影响用户的分析结果**：数字已经算完、
报告已经写完，用户要的东西在内存里好好的。所以 `archive_if_terminal()` 的契约是
"**永不抛异常**"：出任何问题只记一条 warning 并返回 False。调用方（orchestrator、
轮询接口）因此可以无条件地在收口处调用它。

## 为什么可以重复调用

同一个 `workflow_id` 可能被归档两次：orchestrator 跑完归档一次，之后某次轮询发现
心跳过期、用惰性超时把它判成 FAILED 又归档一次（§5.3 的第二种超时路径）。
`HistoryStore.archive()` 是 UPSERT（`ON CONFLICT(workflow_id) DO UPDATE`），
第二次只是"覆盖成最新的样子"，不会长出两条记录。
"""

import logging
from datetime import datetime, timezone

from app.data.history import (
    HistoryRecord,
    get_history_store,
)
from app.schemas_workflow import TERMINAL_STATUSES, WorkflowState

logger = logging.getLogger(__name__)


def build_record(state: WorkflowState) -> HistoryRecord:
    """把一份 `WorkflowState` 抄成一条归档记录（字段映射的唯一实现）。

    两条派生字段的取法值得说明，它们都遵循"派生值现算，不抄可能没同步的字段"：

    - `has_report` 用 `state.report is not None` 现算，**不读 `state.has_report`**：
      和 `WorkflowStatusData.from_state()` 同一个事实来源，免得某个分支只设了
      `report` 忘了同步标志位（§7.1 第 3 条）。
    - `llm_used` 只认 `FinalReport.llm_used`，报告不存在时才是 False（§7.1 第 4 条）。
      另存一份就有"两份不一致"的可能，前端横幅会骗人。

    `finished_at` 的兜底：正常路径上 `_finish()` / `_fail()` / 惰性超时分支都会写它，
    但这里**不假设**。真的为空时用 `archived_at` 顶上 —— 历史列表按 `finished_at`
    排序，留一个 NULL 会让这条记录掉到列表最后，用户看到的是"这次的记录不见了"。
    """
    report = state.report
    archived_at = datetime.now(timezone.utc)

    return HistoryRecord(
        workflow_id=state.workflow_id,
        question=state.question,
        status=state.status.value,
        created_at=state.created_at,
        finished_at=state.finished_at if state.finished_at is not None else archived_at,
        has_report=report is not None,
        llm_used=report.llm_used if report is not None else False,
        report_text=report.model_dump_json() if report is not None else None,
        error_code=state.error.code.value if state.error is not None else None,
        archived_at=archived_at,
    )


def archive_if_terminal(state: WorkflowState) -> bool:
    """终态才归档；返回"这次到底写没写进去"。

    五条规则（顺序就是优先级）：

    1. **不是终态 → 立刻返回 False，一个字都不写库。** 正在跑的工作流没有归档价值：
       它下一秒就可能变成另一个样子，写进去只会让历史列表里出现"还在跑"的记录。
    2. **`finished_at` 为空 → 返回 False，同样一个字都不写库。** 这是 P2 修复
       （详见下面那段"为什么终态还要看 finished_at"）。
    3. 归档库关闭（`DB_PATH` 为空，`get_history_store()` 返回 None）→ 返回 False，
       **这不是错误**：用户主动关掉了这个功能，生产（`DB_PATH` 有值）时才写。
    4. 终态 → `build_record()` + `store.archive()`，成功返回 True。
    5. **任何异常都不许往外抛**（见模块 docstring）→ warning + False。

    调用点在**收口处**（orchestrator 的 `finally`、轮询接口的超时分支），
    而不是每个 `return` 前面各写一遍 —— 分支会漏，收口不会。

    ## 为什么终态还要看 `finished_at`（P2）

    本地验收发现：`build_record()` 会把 `finished_at=None` 兜底成 `archived_at`，
    于是库里**永远不出现 NULL 的 `finished_at`**。这带来两个后果：

    1. `HistoryStore` 那条 `ORDER BY finished_at IS NULL, finished_at DESC` 的
       "NULL 排最后"分支在生产路径上是**死代码** —— 没有坏处，但它是"事实被注释
       说成另一回事"的典型：注释描述的行为没有任何一条路径能触发。
    2. 真正有坏处的是这一条：万一出现 `status` 已终态而 `finished_at` 还为空的畸形
       状态，它会被归档成"**刚刚完成**"（`archived_at`），时间与真相不符；
       更糟的是它落进历史列表最前面，看起来像最新的一条。

    正常产品路径产不出这种状态（实测全仓的 `finished_at` 写入点有 4 处，
    都在把状态收成终态的同一个代码块里；见 `docs/DECISIONS.md` D-26），
    所以这是**防御性**修复，不是已发生故障的修补。

    代价要写清楚：`finished_at` 为空的终态记录现在**根本不入库**，
    于是 `build_record()` 里那句兜底彻底变成"不可达"。
    **代价是"少一条记录"，换来的是"库里的每一条时间都是真的"** ——
    对一个自称"历史归档"的功能，这个交换是划算的；而且丢的是畸形数据，
    它在内存里仍然以终态存在，只是进不了历史列表。
    """
    if state.status not in TERMINAL_STATUSES:
        return False

    # `finished_at is None` + 终态 = 畸形状态（全仓没有一条代码路径能产生它）。
    # 宁可这条记录不进库，也不要给库里塞一个假时间。
    if state.finished_at is None:
        logger.warning(
            "工作流 %s 已是终态 %s 但没有 finished_at，跳过归档（不写入假时间）",
            state.workflow_id,
            state.status.value,
        )
        return False

    try:
        store = get_history_store()
        if store is None:
            return False
        store.archive(build_record(state))
    except Exception as exc:  # noqa: BLE001 —— 契约就是"吞掉一切"，见模块 docstring
        logger.warning(
            "工作流 %s 归档失败（%s: %s），分析结果不受影响",
            state.workflow_id,
            type(exc).__name__,
            exc,
        )
        return False

    return True
