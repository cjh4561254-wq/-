"""工作流查询接口：轮询状态（接口 6）、取报告（接口 7）、取图表 PNG（接口 8）。

这个文件负责什么、不负责什么
--------------------------
负责（**只读 + 两道防线的判断**）：
    把 `registry` 里的 `WorkflowState` 按三个接口的契约读出来 ——
    接口 6 返回精简状态、接口 7 返回报告与原始结果、接口 8 把 PNG 文件送出去。
    整个文件里唯一会**写** `state` 的地方是接口 6 的惰性超时检测，
    为什么必须在这里写、为什么必须先判终态，见 `_expire_if_stale`。

不负责：
    工作流怎么推进（`app/agents/orchestrator.py`）、状态放在哪
    （`app/core/registry.py`）、状态长什么样（`app/schemas_workflow.py`）、
    PNG 怎么画（D5 的 `app/agents/charts.py`）。所以这里没有一行 pandas、
    没有一行 LLM 调用，也**不 mkdir** —— 目录是画图那些代码的事，
    这个文件只负责"有就送出去，没有就说没有"。

三个接口的判据（400 和 404 别写反，写反了前端就没法分流）
------------------------------------------------------
接口 6：工作流不存在 → 404；工作流**自身**失败（含超时）→ 仍然是 **200**。
        查询这次请求是成功的，失败的是被查询的那条工作流，这两件事不能混。
接口 7：工作流不存在 → 404；工作流在、但报告还没生成 → **400**。
        资源是存在的，是**取报告的时机**不对，所以是 400 不是 404。
接口 8：文件名非法（含 `/`、`\\`、`..`，或不是 12 位小写 hex + `.png`）→ **400**；
        文件名合法但磁盘上没有这个文件 → **404**。
        "我请求写错了"和"图还没生成"是两件事，前端要靠状态码区分（§7.0 表）。

错误一律 `raise AppError`，状态码查 `app/errors.py` 的 `ERROR_STATUS`；
这里不写 `JSONResponse`，也不返回 `ok:false` 的 200（§7.0 落码约束第 3 条）。

两个只读历史接口（接口 9 / 10：`GET /workflows/history`、`/workflows/history/{id}`）
--------------------------------------------------------------------------------
它们读的是 SQLite 归档库（`app/data/history.py`），**与内存注册表无关**：
库里查不到就是 404，**禁止**回头去 `registry` 里捞一条补上（§8.3.1 的
"不得 fallback 复活对象"）。那条 fallback 会让"重启后历史还在"这个卖点
变成"有时候在、有时候不在"，而且两条路径的字段来源不同，迟早出现同一个
`workflow_id` 两个版本。

⚠️ **路由顺序**：这两个接口必须声明在 `get_workflow` **之前**。FastAPI 按声明顺序
匹配，`/workflows/history` 和 `/workflows/{workflow_id}` 的路径形状一模一样，
写在后面就会被后者接走、`workflow_id="history"` → 404。`tests/test_history_api.py`
有一条测试专门钉这个（`GET /api/workflows/history` 必须是 200 而不是 404）。
"""

import logging
import re
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from app import config
from app.core import registry
from app.core.archive import archive_if_terminal
from app.data.history import HistoryRecord, get_history_store
from app.errors import AppError
from app.schemas import ApiError, ApiResponse, ErrorCode
from app.schemas_tool import ToolResult
from app.schemas_workflow import (
    TERMINAL_STATUSES,
    FinalReport,
    ValidationResult,
    WorkflowState,
    WorkflowStatus,
    WorkflowStatusData,
)

router = APIRouter(tags=["workflows"])

logger = logging.getLogger(__name__)

# workflow_id 的合法形态：uuid4().hex，32 位小写十六进制（与 app/data/store.py 同款）。
_WORKFLOW_ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

# 图表文件名的合法形态：12 位小写十六进制 + .png。
# 为什么是 12 位：`ChartResult.chart_id` 用 `uuid4().hex[:12]`，而 `ChartResult`
# 自带校验 `filename == chart_id + ".png"`（schemas_tool.py），两边对得上。
# 这个正则同时是第一道路径穿越防线：`..`、`/`、`\\`、盘符、空串全都匹配不上。
_CHART_FILENAME_PATTERN = re.compile(r"\A[0-9a-f]{12}\.png\Z")


class ReportData(BaseModel):
    """接口 7 的返回体：报告 + 校验结果 + 原始工具结果 + "有没有用模型"。

    为什么这个模型写在本文件里，而不是放进 `app/schemas_workflow.py`：
        它只被接口 7 用一次，字段就是把 `state` 里现成的四样东西搬过来；
        而这次的写入范围只允许改三个文件，契约文件不在其中。
        如果以后有第二个接口也要返回它，再搬进 `schemas_workflow.py` 更合适
        （和 `WorkflowStatusData` 并列）。

    `llm_used` 为什么从 `report` 里取而不是单独存：`FinalReport.llm_used`
    是"这次到底有没有用模型"的唯一事实来源（§7.1 第 4 条）。接口另存一份，
    就有"两份不一致"的可能，前端横幅就会骗人。
    """

    report: FinalReport
    validation: ValidationResult | None = None
    results: list[ToolResult] = Field(default_factory=list)
    llm_used: bool = False


class HistoryItem(BaseModel):
    """历史列表与详情共用的一条记录。**字段只来自归档库**，不掺内存态。

    `status` 用 `WorkflowStatus | str`：合法值仍按枚举校验、序列化出来与接口 6
    完全一致（同一个前端函数要能画两种来源的状态）；**认不出来的老值也算合法**，
    原样透出。类型只有写成 `A | str` 才会真的接受裸字符串 —— 只改构造侧的硬转
    （`_coerce_enum`）而不放开这里的字段类型，降级出来的字符串仍会被 pydantic
    拒收、继续 500，用户可见行为一个字都不变。

    `error_code` 同理：前端要按 `WORKFLOW_TIMEOUT` 这类码分流，码的取值域必须与
    接口 6 的 `error.code` 同源，但无法识别的历史码要能原样透出。

    `| str` 是契约的**有意放宽**，不是将就：历史库是跨版本持久化的，枚举改名或
    删值后老数据必然留下当前代码不认识的值，接口的第一职责是别让一条坏行把整页
    打成 500。取证与影响面见 `docs/HISTORY_ROBUSTNESS.md`。
    """

    workflow_id: str
    question: str
    status: WorkflowStatus | str
    created_at: datetime
    finished_at: datetime | None = None
    has_report: bool = False
    llm_used: bool = False
    error_code: ErrorCode | str | None = None


class HistoryListData(BaseModel):
    """接口 9 的返回体：一页记录 + **全库**总数 + 回显分页参数。

    `total` 是 `store.count()`（全库总数），不是 `len(items)`：前端要拿它算
    "还有几页"，给了本页条数就会在第一页之后就开始骗人。

    `enabled=False` 表示归档被关掉了（`DB_PATH` 为空）。这时 HTTP 仍然是 200：
    这不是错误，是"这个功能关着"，前端据此显示一句说明而不是报错。
    """

    items: list[HistoryItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    enabled: bool = True


class HistoryDetailData(BaseModel):
    """接口 10 的返回体：归档记录 + 还原出来的报告（没归档报告时为 None）。"""

    item: HistoryItem
    report: FinalReport | None = None


def _coerce_enum(enum_cls: type[Enum], raw: str) -> Enum:
    """字符串 → 枚举，**认不出来就原样保留字符串**（降级，不抛异常）。

    为什么不能直接 `WorkflowStatus(record.status)`：库里存的是 TEXT，跨版本升级
    （枚举改名/删值）后老数据会留下当前代码不认识的值，硬转抛 `ValueError` 会被
    兜底 handler 翻成 **500 且 `data=null`** —— 一条坏行让同页所有合法记录都拿不
    回来。降级后响应里能看到原样字符串，前端据此显示"无法识别"而不是白屏。

    只在字符串这种**历史文本**上使用；值来自内部常量时不要用。
    取证与影响面见 `docs/HISTORY_ROBUSTNESS.md`。
    """
    try:
        return enum_cls(raw)
    except ValueError:
        return raw  # type: ignore[return-value]


def _parse_report(raw: str) -> FinalReport | None:
    """归档的 `report_text` → `FinalReport`，**坏数据不抛异常、降级成 `None`**。

    这是第三处同类降级（前两处是 `status` / `error_code`，见 `_coerce_enum`）：
    归档的 `report_text` 是**跨版本持久化**的 JSON，报告的 schema 一旦增删必填
    字段，老数据就再也解析不出来。原写法直接 `FinalReport.model_validate_json`
    抛 `ValidationError`，被兜底 handler 翻成 **500** —— 那条记录**永远打不开详情**
    （列表接口不受影响，所以用户看到"能列出、点进去就报错"）。

    降级成 `None`：记录本身、`status`、`error_code`、时间等字段照常返回，前端把
    报告区显示成"这份归档的报告已无法解析"。**不吞错**：原始异常写日志，便于排查。

    注意这是**有意为之的不对称** —— `status` 降级成字符串后用户还能看到值本身，
    而 `FinalReport` 是一棵嵌套结构，降级成 `None` 是唯一不给前端喂半截数据的选项。
    取证见 `docs/HISTORY_ROBUSTNESS.md`。
    """
    try:
        return FinalReport.model_validate_json(raw)
    except ValidationError:
        logger.warning("归档报告无法解析（保留记录、报告置空）", exc_info=True)
        return None


def _item_from_record(record: HistoryRecord) -> HistoryItem:
    """库里的 `HistoryRecord` → 接口模型。

    两个字符串字段（`status` / `error_code`）在这里转回枚举：库里存的是字符串
    （TEXT 列，方便人手查库），接口对外给的是契约里的枚举取值。

    转不动时**降级成原样字符串**（见 `_coerce_enum`），留给用户看，不许 500。
    `error_code` 为 NULL（绝大多数记录）直接短路，不进转换。
    """
    return HistoryItem(
        workflow_id=record.workflow_id,
        question=record.question,
        status=_coerce_enum(WorkflowStatus, record.status),
        created_at=record.created_at,
        finished_at=record.finished_at,
        has_report=record.has_report,
        llm_used=record.llm_used,
        error_code=(
            _coerce_enum(ErrorCode, record.error_code)
            if record.error_code is not None
            else None
        ),
    )


# ---- 接口 9 / 10：历史归档（**必须声明在 `get_workflow` 之前**，见模块 docstring）----


@router.get(
    "/workflows/history",
    response_model=ApiResponse[HistoryListData],
    summary="查询历史归档列表",
)
def list_history(
    limit: int = Query(20, ge=1, le=100, description="每页条数，1–100"),
    offset: int = Query(0, ge=0, description="从第几条开始，从 0 起"),
) -> ApiResponse[HistoryListData]:
    """接口 9：分页读归档库（新→旧）。

    排序在库里定死（`finished_at DESC NULLS LAST, created_at DESC`），接口层不再排：
    分页 + 内存排序是经典的错位来源（每页各自排一遍，跨页顺序就乱了）。

    归档关闭（`DB_PATH` 为空）→ `enabled=False` + 空列表 + **HTTP 200**：
    用户关掉了一个功能，不是他请求错了（对比接口 7 的"报告没生成"是 400：
    那是时机不对，而这里是功能不存在）。
    """
    store = get_history_store()
    if store is None:
        return ApiResponse[HistoryListData](
            ok=True,
            data=HistoryListData(
                items=[], total=0, limit=limit, offset=offset, enabled=False
            ),
            error=None,
        )

    records = store.list_recent(limit=limit, offset=offset)
    return ApiResponse[HistoryListData](
        ok=True,
        data=HistoryListData(
            items=[_item_from_record(record) for record in records],
            total=store.count(),
            limit=limit,
            offset=offset,
            enabled=True,
        ),
        error=None,
    )


@router.get(
    "/workflows/history/{workflow_id}",
    response_model=ApiResponse[HistoryDetailData],
    summary="查询历史归档详情",
)
def get_history_detail(workflow_id: str) -> ApiResponse[HistoryDetailData]:
    """接口 10：读一条归档记录，并把当时的报告还原出来。

    报告是从 `record.report_text`（归档时 `FinalReport.model_dump_json()` 的产物）
    用 `model_validate_json()` 还原的，**不是**从内存里再拿一份 —— 历史记录的意义
    就是"当时是什么样"。还原失败会被 FastAPI 翻成 500：那说明库里的 JSON 被外力
    改坏了，属于真故障，不该假装没这回事。

    查不到 → 404。注意这里**不会**去 `registry` 里找（§8.3.1 禁止 fallback 复活）：
    重启后内存是空的，而历史记录还在 —— 这正是归档存在的理由。
    """
    store = get_history_store()
    record = store.get(workflow_id) if store is not None else None
    if record is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "没有归档记录",
            detail=(
                "这条工作流没有归档记录：归档库只保存跑到终态的工作流，"
                "服务重启后仍在进行中的工作流不会留下历史。"
            ),
        )

    report = (
        _parse_report(record.report_text)
        if record.report_text is not None
        else None
    )
    return ApiResponse[HistoryDetailData](
        ok=True,
        data=HistoryDetailData(item=_item_from_record(record), report=report),
        error=None,
    )


@router.get(
    "/workflows/{workflow_id}",
    response_model=ApiResponse[WorkflowStatusData],
    summary="查询工作流状态",
)
def get_workflow(workflow_id: str) -> ApiResponse[WorkflowStatusData]:
    """接口 6：给前端轮询用的小状态（**故意不含 `report` / `results`**）。

    `workflow_id` 不合法（比如只有 8 位）时**不单独报错**：它不可能在注册表里，
    自然走下面的 404。多写一条"格式不对"的分支，只是多一个要维护的错误消息。

    响应体用 `WorkflowStatusData.from_state()` 生成，**不自己拼 dict**：
    那个方法已经处理了两个容易错的点 —— 只抄前端要的字段、以及现场算
    `has_report = state.report is not None`（派生值不抄 `state.has_report`，
    免得某个分支只设了 report 忘了同步）。
    """
    state = registry.get(workflow_id)
    if state is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "工作流不存在",
            # detail 不回显 workflow_id：它是路径里来的、长度不受控，
            # 回显等于把用户输入原样写进响应体；而且这句话本身已经说清了原因。
            detail="这个 workflow_id 没有登记过。工作流状态存在服务进程的内存里，服务重启后全部丢失。",
        )

    # 轮询是"发现卡死"的唯一时机：没有任何定时器在后台扫，只有人来问的时候才检查。
    _expire_if_stale(state)

    return ApiResponse[WorkflowStatusData](
        ok=True,
        data=WorkflowStatusData.from_state(state),
        error=None,
    )


@router.get(
    "/workflows/{workflow_id}/report",
    response_model=ApiResponse[ReportData],
    summary="获取分析报告",
)
def get_report(workflow_id: str) -> ApiResponse[ReportData]:
    """接口 7：取完整报告（报告可能很大，所以单独一个接口，不塞进轮询）。

    两条分支必须分得开：
        - 工作流不存在 → **404**（资源不存在）；
        - 工作流在、报告还没生成 → **400**（请求的时机不对）。
    这也是前端唯一的判断依据：400 就继续轮询，404 就说明这个 id 彻底没救了。
    """
    state = registry.get(workflow_id)
    if state is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "工作流不存在",
            detail="这个 workflow_id 没有登记过。工作流状态存在服务进程的内存里，服务重启后全部丢失。",
        )

    # 用 `state.report is None` 判断，而不是 `state.has_report`：
    # 与 `WorkflowStatusData.from_state()` 保持同一个事实来源（§7.1 第 3 条）。
    # 这样"接口 6 说 has_report=true，接口 7 却给 400"这种自相矛盾不可能出现。
    if state.report is None:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "报告尚未生成",
            # detail 带当前 status：用户看到"还没好"之外，还知道到底卡在哪一步。
            detail=f"这条工作流当前状态是 {state.status.value}，等它跑完再来取。",
        )

    return ApiResponse[ReportData](
        ok=True,
        data=ReportData(
            report=state.report,
            validation=state.validation,
            results=state.results,
            llm_used=state.report.llm_used,
        ),
        error=None,
    )


@router.get(
    "/workflows/{workflow_id}/charts/{filename:path}",
    response_class=FileResponse,
    summary="获取图表 PNG",
)
def get_chart(workflow_id: str, filename: str) -> FileResponse:
    """接口 8：按文件名把 PNG 送回去。

    路径参数为什么是 `{filename:path}` 而不是 `{filename}`
    ------------------------------------------------
    默认的路径参数匹配不了 `/`（`[^/]+`），所以 `charts/../../evil.png` 这种请求
    会**根本进不到这个函数**，直接由路由层给 404 —— 那我们就无法回答"它是非法输入"
    这件事了。用 `:path` 把整段收下来、再自己校验，才能给出准确的 400。
    两道路径穿越防线（和 `app/data/store.py:63 build_stored_filename` 同一套思路）：

        1. **格式**：`workflow_id` 必须 32 位小写 hex、`filename` 必须是
           12 位小写 hex + `.png`。`..`、`/`、`\\`、盘符一个都进不来。
        2. **落点**：`resolve()` 之后断言文件仍在 `charts_dir` 之下。
           格式检查是"已知的坏写法挡在门外"，这一条是"就算上面那条有洞，
           也不许读到目录外面去"——两道不能互相替代。

    顺序上先校验格式、再查注册表：`abc.png` 无论问哪条工作流都是"请求写错了"，
    报 400 比报 404 更准确（400 与 404 的语义见模块 docstring）。
    """
    if not _WORKFLOW_ID_PATTERN.match(workflow_id) or not _CHART_FILENAME_PATTERN.match(
        filename
    ):
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "图表文件名不合法",
            detail="workflow_id 必须是 32 位小写十六进制，文件名必须是 12 位小写十六进制加 .png。",
        )

    if registry.get(workflow_id) is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "工作流不存在",
            detail="这个 workflow_id 没有登记过。工作流状态存在服务进程的内存里，服务重启后全部丢失。",
        )

    charts_dir = config.get_settings().charts_path
    target = (charts_dir / workflow_id / filename).resolve()
    # 第二道防线：解析完（`.resolve()` 会把 `..` 展开成真实路径）再确认落点。
    # 注意比较的右边也要 resolve()，否则两边的表示形式可能不同（符号链接、
    # 短路径名、大小写），会出现"明明在目录里却判定越界"的假报警。
    if not target.is_relative_to(charts_dir.resolve()):
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "图表路径不合法",
            detail="解析后的路径不在图表目录里，已拒绝。",
        )

    # 文件不存在 → 404（不是 400）：请求是合法的，只是这张图还没生成。
    # 这里**不 mkdir**：目录该由画图的代码创建，路由顺手建目录会把"为什么没有图"
    # 这类问题的现场掩盖掉。
    if not target.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            "图表不存在",
            detail="这张图还没有生成，或者已经被清理掉了。",
        )

    return FileResponse(target, media_type="image/png")


def _expire_if_stale(state: WorkflowState) -> None:
    """惰性超时检测：轮询时发现"太久没心跳"，就地判定超时并落 FAILED。

    为什么放在**接口里**而不是后台定时器里（§5.3）
    ------------------------------------------
    后台线程可能已经死了（进程被杀、线程里出现无法捕获的错误），定时器也就没人
    去重启它；而"用户来轮询"是必然会发生的动作。所以由轮询顺手做一次判断，
    不需要任何额外的调度设施。

    **必须先判终态、再判超时**（顺序写反是本接口最恶劣的 bug）
    ------------------------------------------------------
    一条已经 `SUCCESS` 的工作流跑完后就不再有理由刷新 `heartbeat_at` 了 ——
    如果先判超时，3 分钟后的一次普通轮询会把它**改判成 FAILED**：
    结果是好的，接口却告诉用户"失败了"。这类 bug 只在"服务多跑了三分钟之后"
    才出现，本地手点一遍根本碰不到。所以第一件事就是：终态直接返回，什么都不做。

    这里只改三个字段，不碰别的（也**不加锁**，理由同 registry 的规则 1）：
    `status` / `error` / `finished_at` —— 把这条工作流收成一个"结束了"的样子，
    免得前端轮询到一个永远停在 EXECUTING 的状态。
    """
    if state.status in TERMINAL_STATUSES:
        # 已经结束了：心跳是历史数据，不再是"卡死"的证据。
        return

    if state.heartbeat_at is None:
        # 还没开始跑（后台线程没启动、或还没来得及刷第一次心跳）。
        # 这时候 created_at 到现在的时长由 orchestrator 自己的超时判断管，
        # 不该由轮询接口来下判决。
        return

    settings = config.get_settings()
    now = datetime.now(timezone.utc)
    idle_seconds = (now - _as_utc(state.heartbeat_at)).total_seconds()
    if idle_seconds <= settings.max_workflow_seconds:
        return

    state.status = WorkflowStatus.FAILED
    state.error = ApiError(
        code=ErrorCode.WORKFLOW_TIMEOUT,
        message="分析超时",
        # 说清"多久没动静了"与"上限是多少"，否则用户只看到一句"超时"没法判断
        # 是自己网络慢还是服务卡了。措辞与 orchestrator 的超时分支保持一致。
        detail=(
            f"心跳已有 {int(idle_seconds)} 秒没有刷新，超过上限 "
            f"{settings.max_workflow_seconds} 秒，判定为超时。"
        ),
    )
    state.finished_at = now
    # 注意：这里**不抛异常**。工作流超时不是"这次查询失败"，
    # 接口照样返回 HTTP 200，状态与 error.code 说清发生了什么（§7.0 表）。
    # ERROR_STATUS 里 WORKFLOW_TIMEOUT 映射的就是 200，这两处是同一个约定。
    #
    # 归档：惰性超时是**第二条到达终态的路径**（第一条是 orchestrator 跑完的
    # `finally`），所以这里也必须写库 —— 否则一条卡死的工作流永远不会出现在
    # 历史列表里，用户重启服务后连"它曾经卡住过"都查不到。
    # `archive_if_terminal()` 自己判终态、自己吞异常；同一条被归档两次也没关系，
    # `HistoryStore.archive()` 是 UPSERT（模块 docstring 的"为什么可以重复调用"）。
    archive_if_terminal(state)


def _as_utc(value: datetime) -> datetime:
    """给朴素时间补上 UTC 时区，避免 `TypeError`。

    测试里手写 `datetime(2026, 10, 13)` 是很自然的写法，而**朴素时间减带时区时间
    会直接抛 TypeError**。那会把"判断有没有超时"变成"接口 500"，所以这里补一次
    （和 `app/agents/orchestrator.py:314` 的 `_timed_out` 同一个处理）。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
