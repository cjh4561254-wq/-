"""分析接口：受理一个问题，立刻把 `workflow_id` 交回去，让前端去轮询。

这个文件负责什么、不负责什么
---------------------------
负责（**只做接线**）：
    校验 `dataset_id` 存在 → 建一条 `WorkflowState`（PENDING）→ 登记进 registry →
    把执行任务交给 `BackgroundTasks` → 立刻返回 202。**这里不跑分析。**

不负责：
    分析怎么跑（`app/core/background.py` + `app/agents/orchestrator.py`）、
    工作流放在哪（`app/core/registry.py`）、状态长什么样（`app/schemas_workflow.py`）。
    所以这个文件里没有一行 pandas，也没有一行 LLM 调用。

为什么这里的 router 没有 prefix（和 `routes_datasets.py` 的写法不同）
------------------------------------------------------------------
接口路径在架构文档 §7.0 的状态码表里写死是 `/api/analysis`，而 §8.2 零件 3 与
§4.14.3 给出的装饰器原文就是 `@router.post("/analysis", ...)`。所以 router
**不能**再写 `prefix="/analysis"` —— 那会拼成 `/api/analysis/analysis`。
`/api` 这一段由 `app/main.py` 统一挂。

错误处理只有一种写法：`raise AppError`，状态码查 `app/errors.py` 的 `ERROR_STATUS` 表
--------------------------------------------------------------------------------
`dataset_id` 不存在 → 404 `NOT_FOUND`（§7.0 表：资源不存在就是真实 404）。
这里**不写 `JSONResponse(status_code=...)`**，也**不返回 `ok:false` 的 200** ——
§7.0 的落码约束第 3 条明确禁止：那样 HTTP 层就在撒谎，前端只能靠解析响应体才知道
"没找到"，而 404 一眼就能看出来。
"""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks

from app.core import registry
from app.core.background import submit
from app.data.store import store
from app.errors import AppError
from app.schemas import ApiResponse, ErrorCode
from app.schemas_workflow import (
    AnalysisAccepted,
    QuestionRequest,
    WorkflowState,
    WorkflowStatus,
)

router = APIRouter(tags=["analysis"])


@router.post(
    "/analysis",
    status_code=202,
    response_model=ApiResponse[AnalysisAccepted],
    summary="提交分析问题",
)
def start_analysis(
    req: QuestionRequest, background: BackgroundTasks
) -> ApiResponse[AnalysisAccepted]:
    """受理一个分析问题：登记工作流后**立刻**返回，不等分析跑完。

    `status_code=202` 是 §7.0 表里指定的一行：语义是"已受理"，不是"已完成"。
    写成默认的 200 会让前端以为"结果已经有了"，而此刻后台线程可能还没启动。

    返回体只放 4 个字段（`AnalysisAccepted.from_state`），**不回 `state` 的完整
    dump**：`results` / `plan` 这些字段可能有几百行，而前端拿到 `workflow_id`
    就会去轮询接口 6；多传的部分纯属浪费，还会把内部字段变成对外契约。
    """
    # ① 数据集必须真的在内存里。这里返回 404 而不是"受理后失败"：用户拿一个
    #    不存在的 dataset_id 提问，是请求本身错了，当场告诉他比让他轮询更省事
    #    （而且后台线程拿不到 DataFrame，也只能落一条 FAILED，多绕一圈）。
    if not store.exists(req.dataset_id):
        raise AppError(
            ErrorCode.NOT_FOUND,
            "数据集不存在",
            # detail 里不回显 dataset_id：它是请求体里来的、长度不受控，
            # 回显等于把用户的输入原样写进响应体，没必要。
            detail="这个 dataset_id 没有登记过。数据存在服务进程的内存里，服务重启后全部丢失。",
        )

    # ② workflow_id 必须是 `uuid4().hex`（32 位小写、**没有连字符**）：
    #    `WorkflowState.workflow_id` 上有 `^[0-9a-f]{32}$` 的正则，`str(uuid4())`
    #    会被当场拒掉 —— 这条检查在模型上，不靠"调用方记得写对"。
    state = WorkflowState(
        workflow_id=uuid4().hex,
        dataset_id=req.dataset_id,
        question=req.question,
        status=WorkflowStatus.PENDING,
        step="profiling",
        created_at=datetime.now(timezone.utc),
        max_steps=req.max_steps,
    )

    # ③ 先登记、再排队。顺序反过来的话，后台线程可能比登记跑得还快，
    #    前端轮询时会短暂地"查无此工作流"（404），看起来像提交失败。
    registry.create(state)

    # ④ 登记后台任务后立刻返回：`BackgroundTasks` 会在响应发出去**之后**才执行，
    #    而且 `run_workflow` 是同步函数，Starlette 会把它丢进线程池。
    submit(state, background)

    return ApiResponse[AnalysisAccepted](
        ok=True,
        data=AnalysisAccepted.from_state(state),
        error=None,
    )
