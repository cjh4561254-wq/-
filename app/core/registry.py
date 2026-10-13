"""工作流状态注册表：在进程内保存"正在跑的工作流"，供轮询接口读取。

## 为什么是进程内 dict，而不是数据库 / Redis（架构文档 §8.2.1）

这个项目是**单进程部署**的演示系统：一个 uvicorn 进程、一个演示者。
工作流最多活 180 秒（§5.3 的 `max_workflow_seconds`），服务重启后本来就该重来。
为了这点需求引入 Redis 或数据库，等于给"跑起来"多加一道前置条件（要装、要配、要启），
换来的只是"重启后还能查到已结束的工作流"——而那个功能没人要。

## 两条写死的规则（违反了会出**很难查**的 bug，所以单独说清楚）

**规则 1：`_lock` 只保护 `_workflows` 这个 dict 的读-改-写，绝不给字段赋值加锁。**

`WorkflowState` 是 Pydantic 模型，后台线程会一路原地修改它（改 `status`、追加 `results`……）。
如果把"改 state 的字段"也纳入同一把锁，那么"持锁 → 改 state → 触发某个回调 → 又要拿锁"
就会自己等自己；锁的范围一旦扩散到 Orchestrator，死锁只是时间问题。

不加锁是安全的，理由有两条：
1. 后台线程是**顺序**推进一条工作流的，不存在两个线程同时写同一个 `state`；
2. CPython 里单个属性的赋值是原子的，前端读到的要么是旧值、要么是新值，
   最坏也只是"慢一拍"，不会读到写了一半的对象。

**规则 2：`get()` 必须返回同一个对象引用，不许 `model_copy()` / `deepcopy()`。**

轮询接口 `GET /api/analysis/{workflow_id}` 靠 `get()` 拿状态，而后台线程更新的是
注册表里**那个**对象。如果这里返回副本，后台线程更新的是副本，前端轮询到的永远是
创建时的快照——表现是"进度条卡在 PROFILING 不动"，而且**没有任何异常和日志**，最难查。
"""

import threading
from datetime import datetime, timezone

from app.schemas_workflow import WorkflowState

# 注册表本体。除了本模块的函数，别的地方一律不许直接碰它
#（尤其不要绕过 create() 往里塞，否则以后换存储实现时找不到所有写入点）。
_workflows: dict[str, WorkflowState] = {}

# 只保护 _workflows 的读-改-写，不保护 state 的字段（见模块 docstring 规则 1）。
_lock = threading.Lock()


def create(state: WorkflowState) -> None:
    """登记一条工作流。同一个 `workflow_id` 再 create 会**覆盖**旧的，不报错。

    为什么允许覆盖：`workflow_id` 是 `uuid4().hex`，撞号的概率可以忽略；
    而"重复登记"最可能的场景是测试里手工造了同一个 id。这里报错只会让调用方
    多写一层判断，收益为零。
    """
    with _lock:
        _workflows[state.workflow_id] = state


def get(workflow_id: str) -> WorkflowState | None:
    """按 id 取工作流；取不到返回 `None`（**不抛异常**，调用方要用它判断 404）。

    返回的是注册表里的**原对象**，调用方（含后台线程）改它，前端下一次轮询就能看到。
    这是刻意的设计，不是偷懒——见模块 docstring 规则 2。
    """
    with _lock:
        return _workflows.get(workflow_id)


def touch(workflow_id: str) -> None:
    """刷新心跳时间（`heartbeat_at = 现在`）。**找不到就安静返回，不抛异常。**

    心跳是"锦上添花"：它只用来判断"这条工作流是不是卡死了"。
    如果为了一个不存在（或已被清理）的 id 抛异常，调用方就得包一层 try，
    而它本来只是想说一句"我还活着"。**注意锁的范围**：这里只在 `get()` 里加锁，
    字段赋值在锁外做（规则 1）。
    """
    state = get(workflow_id)
    if state is None:
        return
    state.heartbeat_at = datetime.now(timezone.utc)


def list_all() -> list[WorkflowState]:
    """返回所有工作流的**快照列表**（新列表，里面的元素还是原对象）。

    为什么返回新列表：调用方会排序、切片、过滤。如果直接把内部 dict 的 view 给出去，
    调用方一 `sort()` 就改了内部顺序，一 `append()` 就凭空多出一条工作流——
    这类 bug 只会在某个不起眼的调用点出现。新列表的代价可以忽略（同时最多几条工作流）。
    """
    with _lock:
        return list(_workflows.values())


def clear() -> None:
    """清空注册表。**只给测试用**。

    生产代码里调用它等于让所有在跑的工作流瞬间失联（前端从"进度 60%"变成 404）。
    测试需要它是因为注册表是**进程内全局状态**：上一条用例留下的工作流会污染下一条，
    而这种污染在测试顺序变化时才会暴露，排查成本极高。
    """
    with _lock:
        _workflows.clear()


def count() -> int:
    """当前登记了多少条工作流（测试与调试用，比 `len(list_all())` 少复制一次列表）。"""
    with _lock:
        return len(_workflows)
