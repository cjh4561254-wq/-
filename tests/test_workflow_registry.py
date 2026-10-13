"""`app/core/registry.py` 的测试：同一对象引用、心跳、快照、并发不丢。

## 为什么这几条值得写

这个注册表是"前端轮询能看到进度"的**唯一**依赖。它坏掉的方式都很安静：

- `get()` 返回副本 → 后台线程更新副本，前端永远停在 PROFILING，**没有异常、没有日志**；
- `_lock` 范围写错（把字段赋值也包进去）→ 平时看不出问题，并发一上来才死锁；
- `create` 忘了加锁 → 100 个线程同时写 dict，偶发少几条，复现概率取决于机器。

这三类问题都不会在"手工点一遍"里暴露，所以必须用测试钉住。
"""

import threading
import time
from datetime import datetime, timezone

from app.core import registry
from app.schemas_workflow import WorkflowState


def _make_state(workflow_id: str = "a" * 32) -> WorkflowState:
    """造一条最小可用的 `WorkflowState`（必填字段只有 4 个）。"""
    return WorkflowState(
        workflow_id=workflow_id,
        dataset_id="ds-1",
        question="各地区的销售额怎么样",
        created_at=datetime.now(timezone.utc),
    )


def setup_function() -> None:
    """每条用例前清空注册表。

    注册表是**进程内全局状态**，上一条用例留下的工作流会污染下一条。
    这种污染只在"测试顺序变了"或"单独跑某一条"时才暴露，排查成本极高 ——
    所以宁可每条用例都从空开始。
    """
    registry.clear()


def teardown_function() -> None:
    """每条用例后也清空（**不能只靠 setup**）。

    只清"前面"是不够的：最后一条用例登记的工作流会活到**整个测试会话结束**，
    于是别的测试文件（或同文件的反序执行）看到的是一个非空的全局注册表。
    QA 实测过这个差异：正序跑完 `workflows_left=0`，反序跑完 `workflows_left=1` ——
    两次都是绿灯，但"用例之间已经通过全局状态连在一起了"。

    这就是**顺序耦合**：今天不出错只是因为碰巧没有别的文件去数注册表里有几条。
    以后 `routes_analysis.py` 的测试要断言"注册表里有 1 条工作流"时，
    它会因为这里漏下的一条而莫名其妙地红，而排查方向会完全跑偏（去看路由代码）。
    """
    registry.clear()


def test_create_then_get_returns_the_same_object() -> None:
    """`get()` 必须返回**同一个对象引用**（架构文档 §8.2.1 规则 2）。

    用 `is` 而不是 `==`：`==` 对两个内容相同的副本同样成立，
    而"内容相同"恰恰是出问题时的表现（后台改了原对象、前端拿到副本）。
    """
    state = _make_state()
    registry.create(state)

    assert registry.get(state.workflow_id) is state


def test_get_missing_id_returns_none() -> None:
    """不存在的 id → `None`。接口层要靠它判断 404，不能抛异常。"""
    assert registry.get("f" * 32) is None


def test_touch_missing_id_is_silent() -> None:
    """`touch()` 一个不存在的 id → 安静返回，不抛异常。

    心跳是"锦上添花"：轮询一个已经结束、已被清理的工作流不该让接口 500。
    """
    registry.touch("f" * 32)  # 不抛异常即通过


def test_touch_refreshes_heartbeat() -> None:
    """`touch()` 真的会刷新 `heartbeat_at`（这是超时检测的唯一依据）。"""
    state = _make_state()
    state.heartbeat_at = datetime.now(timezone.utc)
    registry.create(state)
    before = state.heartbeat_at

    time.sleep(0.01)  # 睡够 10ms：Windows 上 datetime.now() 的间隔实测 ≥10.1ms
    registry.touch(state.workflow_id)

    assert state.heartbeat_at is not None
    assert state.heartbeat_at > before


def test_touch_does_not_create_a_missing_workflow() -> None:
    """`touch()` 不能顺手把工作流"造"出来：它只该刷新已存在的。

    这条防的是实现写成 `_workflows.setdefault(workflow_id, ...)` 之类的写法 ——
    那样一次误传 id 的轮询就会凭空多出一条空工作流，`count()` 从此对不上。
    """
    registry.touch("f" * 32)

    assert registry.count() == 0


def test_list_all_returns_a_new_list() -> None:
    """`list_all()` 返回**快照列表**：改它不影响注册表内部。

    调用方会排序、切片、过滤。如果给出去的是内部 dict 的 view，
    调用方一次 `sort()` 就改了内部顺序，一次 `append()` 就凭空多一条工作流。
    """
    registry.create(_make_state("a" * 32))
    registry.create(_make_state("b" * 32))

    snapshot = registry.list_all()
    assert len(snapshot) == 2

    snapshot.clear()  # 调用方乱改返回的列表
    snapshot.append(_make_state("c" * 32))

    assert registry.count() == 2  # 内部纹丝不动
    assert len(registry.list_all()) == 2


def test_concurrent_create_does_not_lose_any_workflow() -> None:
    """100 个线程同时 `create`，一条都不能丢。

    这是"锁到底有没有用"的唯一证据：把 `create` 里的 `with _lock:` 去掉，
    这条用例在 CPython 上仍然**很可能**通过（GIL 让 `dict.__setitem__` 看起来是原子的），
    但它证明了"我们不是靠运气"——真正的并发压力来自 uvicorn 的线程池，
    而这里至少要覆盖住"多线程调用同一个入口"这件事。

    用 `Barrier` 而不是"启动完就 join"：后者会让线程一个接一个地跑完，
    变成 100 次串行调用，测不到并发。Barrier 让 100 个线程在同一瞬间一起冲进 `create`。
    """
    thread_count = 100
    barrier = threading.Barrier(thread_count)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)  # 所有线程就位后才一起写
            registry.create(_make_state(f"{index:032x}"))
        except BaseException as exc:  # noqa: BLE001 —— 子线程里的异常不会让 pytest 变红，必须自己收
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert registry.count() == thread_count
    assert len(registry.list_all()) == thread_count


def test_clear_empties_the_registry() -> None:
    """`clear()` 是测试专用入口，行为要明确（免得测试自己互相污染）。"""
    registry.create(_make_state())

    registry.clear()

    assert registry.count() == 0
    assert registry.list_all() == []
