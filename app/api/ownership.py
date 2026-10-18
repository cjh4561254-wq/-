"""归属判定：把"这条数据是不是当前登录用户的"收在一处。

## 为什么单独一个模块（而不是在每个路由里写四行 if）

本次改造要给三类资源加数据隔离：数据集（进程内）、工作流状态（进程内注册表）、
历史归档（SQLite）。三类资源分散在四个路由文件里，一共八九个接口。如果每个接口
各写一遍 `if item.owner_user_id != user.id: raise AppError(...)`，会有两个后果：

1. **漏一个就是越权漏洞**，而漏掉的那个接口不会有任何测试失败提醒你；
2. **报错文案会漂移**：有的接口写"数据集不存在"、有的写"无权访问"，
   而后者等于告诉攻击者"这个 id 是存在"的（可以拿它枚举出系统里有哪些数据）。

所以这里的两个函数就是**唯一**的判定入口。加新接口时照着调用，
不要自己写比较 —— 需要新资源类型时，在这里加一个函数。

## 为什么不返回 403（写给未来的自己）

403 的语义是"我知道你要什么，但你不许" ——它**承认了资源存在**。
对"别人的数据"来说，正确的回答是假装它不存在（404），理由有两条：

- **不可枚举**：攻击者拿不到"哪些 id 是真的"这个信息，只能一个个试，
  而试错的响应和自己输错 id 完全一样，没有任何信号；
- **前端更简单**：前端只需要处理一种情况（"这条数据没了/不是你的 → 回列表页"），
  不需要为 403 单独设计一个"你越权了"的页面。

这不是"信息隐藏做过头"：本项目所有 id 都是 uuid4（128 位随机），
猜不到；真正会泄露存在性的是错误文案的差异，所以两种情况的文案必须**逐字相同**。
"""

from app.data.history import HistoryRecord
from app.errors import AppError
from app.schemas import ErrorCode
from app.schemas_dataset import Dataset
from app.schemas_workflow import WorkflowState

# 数据集不存在 / 不属于你，用**同一份**文案与 detail。
# 写成模块级常量（而不是在函数里现拼字符串）是为了让"两处必须一致"这件事
# 有一个可被 grep 的、唯一的定义点 —— 复制粘贴两份字符串迟早会改歪一份。
_DATASET_NOT_FOUND_MESSAGE = "数据集不存在"
_DATASET_NOT_FOUND_DETAIL = (
    "这个 dataset_id 没有登记过，或者它不属于当前登录用户。"
    "数据存在服务进程的内存里，服务重启后全部丢失。"
)

_WORKFLOW_NOT_FOUND_MESSAGE = "工作流不存在"
_WORKFLOW_NOT_FOUND_DETAIL = (
    "这个 workflow_id 没有登记过，或者它不属于当前登录用户。"
    "工作流状态存在服务进程的内存里，服务重启后全部丢失。"
)

_HISTORY_NOT_FOUND_MESSAGE = "没有归档记录"
_HISTORY_NOT_FOUND_DETAIL = (
    "这条工作流没有归档记录，或者它不属于当前登录用户："
    "归档库只保存跑到终态的工作流，服务重启后仍在进行中的工作流不会留下历史。"
)


def require_owned_dataset(dataset: Dataset | None, user_id: int) -> Dataset:
    """数据集必须是当前用户的，否则 404。

    `dataset` 为 `None`（从来没登记过）与"登记过但属于别人"走**同一条分支**：
    这是本模块的核心约定，任何改动都必须保持两者响应完全一致。

    `owner_user_id` 为 `None` 的老数据同样判 404 ——
    "没有归属"不等于"大家的"，见 `app/data/store.py:list_owned` 的说明。
    """
    if dataset is None or dataset.owner_user_id != user_id:
        raise AppError(
            ErrorCode.NOT_FOUND,
            _DATASET_NOT_FOUND_MESSAGE,
            detail=_DATASET_NOT_FOUND_DETAIL,
        )
    return dataset


def require_owned_workflow(state: WorkflowState | None, user_id: int) -> WorkflowState:
    """工作流必须是当前用户的，否则 404（含"根本不存在"）。"""
    if state is None or state.owner_user_id != user_id:
        raise AppError(
            ErrorCode.NOT_FOUND,
            _WORKFLOW_NOT_FOUND_MESSAGE,
            detail=_WORKFLOW_NOT_FOUND_DETAIL,
        )
    return state


def require_owned_history(
    record: HistoryRecord | None, user_id: int
) -> HistoryRecord:
    """归档记录必须是当前用户的，否则 404（含"根本不存在"）。

    防线的**位置**要特别说明：这里判的是"查出来的这条是不是你的"，
    但真正让别人的记录**根本查不出来**的是数据层 —— 路由查库时必须传
    `owner_user_id=`（`HistoryStore.get_for_user()` / `list_recent()` / `count()`）。
    两层都要有：

    - 数据层按 owner 过滤，别人的记录不进入内存（也不可能被误用）；
    - 这里再做一次判断，覆盖"数据层将来被人改成不带过滤"的情况。

    只靠这里是不够的：如果查询没过滤、只是在这一层判 404，
    那么 `total` 之类的聚合值仍会把别人的数据量泄露出去。

    `owner_user_id` 为 `None`（老库里的行没有归属）同样判 404：
    见 `app/data/history.py` 的 `HistoryRecord` 说明。
    """
    if record is None or record.owner_user_id != user_id:
        raise AppError(
            ErrorCode.NOT_FOUND,
            _HISTORY_NOT_FOUND_MESSAGE,
            detail=_HISTORY_NOT_FOUND_DETAIL,
        )
    return record
