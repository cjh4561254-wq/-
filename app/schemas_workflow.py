"""工作流的全部数据结构：计划、任务、状态、报告、校验结果。

这个文件为什么存在（B-1 修订的落地点）
--------------------------------------
架构文档 §4.0 写死了一条判据：

    "数据长什么样"  → 进 `schemas_*.py`
    "数据放在哪、怎么同步" → 进 `core/`

`WorkflowState` 虽然和"运行状态"有关，但它回答的是"**一条工作流长什么样**"，
所以它定义在**这里**，`app/core/registry.py` 里一行模型定义都没有。
好处很直接：读者搜 `class WorkflowState` 一定搜得到，
不用先去猜"这个模型是不是藏在某个 registry 里"。

第 3 天只定义、不使用
---------------------
今天（D3）这个文件的作用是**冻结契约**：工具层、调度层、接口层三方对着
同一份模型写代码，谁都不用等谁。真正用到这些模型的代码在 D4（orchestrator /
planner / routes_analysis）与 D5（charts / validation）。

一份数据流全景（读代码时对着这张图看）
--------------------------------------
    POST /api/analysis
        └─ 建一个 WorkflowState（status=PENDING）
             ├─ PROFILING   → state.profile  = DatasetProfile   （D2 已有）
             ├─ PLANNING    → state.plan     = TaskPlan          （D4 调模型）
             ├─ EXECUTING   → state.results += ToolResult        （D3 工具，D4 调度）
             ├─ CHARTING    → state.charts  += ChartResult       （D5）
             ├─ REPORTING   → state.report   = FinalReport       （D5 调模型）
             └─ VALIDATING  → state.validation = ValidationResult（D5 纯代码）
        └─ 终态：SUCCESS / SUCCESS_WITH_WARNINGS / FAILED

**只有 Orchestrator 能改 `state`**（架构文档 §3 第 1 条）。子 Agent 收到的是
普通参数，返回的是新的结果对象；把结果写进 state 的动作由编排层做。
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas import ApiError
from app.schemas_dataset import DatasetProfile
from app.schemas_tool import ChartResult, ChartType, ToolResult


class TaskStatus(str, Enum):
    """单个分析任务的状态。

    为什么用 `str, Enum` 而不是裸 `Enum`：
        JSON 里会出现这些值，继承 str 之后 `TaskStatus.SUCCESS == "SUCCESS"`
        成立，前端直接拿到字符串，不用额外做一次 `.value` 转换。
    """

    PENDING = "PENDING"  # 已进入计划，还没执行
    RUNNING = "RUNNING"  # 正在执行（同一时刻最多一个）
    SUCCESS = "SUCCESS"  # 工具跑通且返回结构校验通过
    FAILED = "FAILED"  # 校验拒绝或工具抛异常，重试已用尽
    RETRYING = "RETRYING"  # 失败后正在重试
    SKIPPED = "SKIPPED"  # 依赖的任务失败了，被跳过


class WorkflowStatus(str, Enum):
    """整条工作流的粗粒度状态。前端靠它决定"还要不要继续轮询"。

    `step`（见 `WorkflowState.step`）是"此刻具体在做什么"，
    `status` 是"对外怎么描述"。两者不是一回事：
    `status=EXECUTING` 期间，`step` 可能是 `execution`，也可能正在重试某个任务。
    """

    PENDING = "PENDING"
    PROFILING = "PROFILING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    CHARTING = "CHARTING"
    REPORTING = "REPORTING"
    VALIDATING = "VALIDATING"
    SUCCESS = "SUCCESS"
    SUCCESS_WITH_WARNINGS = "SUCCESS_WITH_WARNINGS"
    FAILED = "FAILED"


# 到达这三个状态就不用再轮询了。定义成模块级常量而不是散在各处写 `in (...)`：
# 前端、接口、测试三处都要用同一个集合，写错一处就会多转几秒或永远转下去。
TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.SUCCESS, WorkflowStatus.SUCCESS_WITH_WARNINGS, WorkflowStatus.FAILED}
)

# 阶段名。`WorkflowState.step` 只能取这些值，前端拿它查中文文案。
WorkflowStep = Literal[
    "profiling",
    "planning",
    "execution",
    "charting",
    "reporting",
    "validation",
    "done",
]


class ChartRequest(BaseModel):
    """planner 给某个任务配的"顺手出张图"指令。

    注意这里只有**参数**，没有数据：图表 Agent 会拿着 `source_task_id`
    回到 `ToolResult.data` 里去取数，绝不接受模型直接给的数字点
    （架构文档 §3.4 的"不能做"第 ③ 条）。
    """

    chart_type: ChartType = Field(description="柱状 / 折线 / 饼图 / 散点")
    x_field: str = Field(description="X 轴字段名，必须是工具返回结果里的列")
    y_field: str = Field(description="Y 轴字段名，必须是工具返回结果里的列")
    title: str = Field(max_length=40, description="图标题，≤40 字，超出由 planner 截断")


class AnalysisTask(BaseModel):
    """一个分析任务：用哪个工具、带什么参数、要不要出图。

    这是**模型与真实计算之间的唯一接口**：模型只能填这张表，
    填完之后的执行阶段一行 LLM 调用都没有（架构文档 §3.3）。
    """

    task_id: str = Field(pattern=r"^T[0-9]{1,2}$", description="T1 / T2 …… 报告引用它做溯源")
    order: int = Field(ge=1, description="执行顺序，从 1 开始且连续")
    kind: Literal["describe", "aggregate", "compare", "trend", "missing", "correlation"] = Field(
        description="语义类别（给人看，也用于默认图表选择）"
    )
    tool_name: str = Field(description="工具名，**必须在白名单里**，否则任务被丢弃")
    params: dict[str, Any] = Field(
        default_factory=dict, description="工具参数，键名必须与工具的 params_schema 完全一致"
    )
    description: str = Field(description="一句中文说明，例：按 地区 汇总 销售额，取前 5")
    depends_on: list[str] = Field(
        default_factory=list, description="依赖的 task_id；依赖的任务失败时本任务变 SKIPPED"
    )
    chart: ChartRequest | None = Field(default=None, description="要不要出图，默认不出")

    # 下面三个是**运行期**字段：planner 生成计划时是默认值，由 Orchestrator 推进。
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    attempt: int = Field(default=0, ge=0, le=2, description="已重试次数，最多 2 次")
    error: str | None = Field(default=None, description="失败原因（中文），给前端显示")


class TaskPlan(BaseModel):
    """一次规划的结果。`source` 字段是"这次计划可不可信"的钥匙。"""

    plan_id: str = Field(description="uuid4().hex[:8]")
    question: str = Field(description="用户原问题，原样保存便于复盘")
    dataset_id: str = Field(description="针对哪个数据集")
    tasks: list[AnalysisTask] = Field(min_length=1, max_length=8, description="1–8 个任务")
    reasoning: str | None = Field(default=None, description="模型给的一句话规划理由，只做展示")
    source: Literal["llm", "rule_fallback"] = Field(
        description="计划来自模型还是规则兜底。**必须落日志**，便于统计模型稳定性"
    )
    created_at: datetime = Field(description="生成时间（UTC）")


class ValidationIssue(BaseModel):
    """校验发现的一个问题。`code` 是给程序看的，`message` 是给人看的。"""

    severity: Literal["error", "warning", "info"] = Field(description="error 会让 passed=False")
    code: Literal[
        "UNSUPPORTED_NUMBER",  # 报告里出现了工具结果里找不到的数字（编造）
        "UNKNOWN_COLUMN",  # 报告里提到了画像里不存在的列名
        "EMPTY_RESULT",  # 某个任务返回 0 行
        "FAILED_TASK",  # 某个任务失败了
        "MISSING_CHART_FILE",  # 图表对象存在但 PNG 文件不在磁盘上
        "SHORT_REPORT",  # 报告内容过短，像没写完
    ] = Field(description="机器码")
    message: str = Field(description="中文说明，直接给前端显示")
    task_id: str | None = Field(default=None, description="问题归属的任务；不是任务问题时为 None")


class ValidationResult(BaseModel):
    """校验 Agent 的产出。**它是"报告里的数字能核对"这句话的实现**。"""

    passed: bool = Field(description="没有 error 级问题即为 True")
    checked_numbers: int = Field(ge=0, description="一共检查了多少个数字，用来给出一个确实查过的量")
    unsupported_numbers: list[str] = Field(
        default_factory=list, description="在工具结果里找不到出处的数字（防编造的核心产出）"
    )
    issues: list[ValidationIssue] = Field(default_factory=list, description="问题明细")
    checked_at: datetime = Field(description="校验时间（UTC）")


class ReportSection(BaseModel):
    """报告里的一段。`table` 是**直接复制**工具结果的行，不是重新算的。"""

    heading: str = Field(description="小标题")
    text: str = Field(description="正文（模型写的解释，数字必须来自 results）")
    cites: list[str] = Field(default_factory=list, description="依据的 task_id 列表，例 ['T1','T3']")
    table: list[dict[str, Any]] | None = Field(
        default=None,
        description="直接复制 ToolResult.data['rows'] 的前 20 行，保证网页表格与工具输出完全一致",
    )


class FinalReport(BaseModel):
    """最终分析报告。`llm_used` 是"这次到底有没有用模型"的诚实标记。"""

    report_id: str = Field(description="uuid4().hex[:8]")
    title: str = Field(description="报告标题，例：销售额按地区分析")
    summary: str = Field(description="3–5 句总述，数字必须来自 results")
    sections: list[ReportSection] = Field(min_length=1, description="至少 1 段，通常 3 段")
    data_quality_notes: list[str] = Field(
        default_factory=list, description="直接抄 DatasetProfile.warnings，不由模型编"
    )
    citations: list[str] = Field(default_factory=list, description="命中的 task_id 列表")
    generated_at: datetime = Field(description="生成时间（UTC）")
    llm_used: bool = Field(
        description="是否真的调了模型。未配 LLM 时为 False，前端要显示规则模式提示"
    )


class WorkflowState(BaseModel):
    """一条工作流的全部状态。**这是整个系统唯一的事实来源。**

    为什么用 Pydantic 模型而不是一个普通 dict：
        ① 字段名写错会当场报错，而不是悄悄多出一个没人读的键；
        ② 接口直接 `model_dump(mode="json")` 就能返回，不用手写序列化；
        ③ 新人读代码时，"一条工作流有哪些字段"一眼可见。

    为什么可以放心就地修改（`state.status = ...`）：
        Pydantic 默认不做不可变约束，Orchestrator 就是靠原地更新推进流程的。
        **但只有 Orchestrator 有权改**，子 Agent 只返回新对象（架构文档 §3 第 1 条）。

    与 `core/registry.py` 的分工（别搞混）：
        这个类回答"一条工作流长什么样"；
        registry 回答"这些工作流放在哪、怎么并发安全地取"。
    """

    workflow_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="uuid4().hex，对外主键")
    dataset_id: str = Field(description="关联的数据集")
    question: str = Field(min_length=1, max_length=500, description="用户问题，1–500 字")

    # 归属：发起这条工作流的用户。用途只有数据隔离（别人轮询不到、也拿不到报告）。
    #
    # 为什么默认 None（而不是必填）：`WorkflowState` 在测试、探针脚本、以及
    # 本次改造之前的代码里被大量直接构造；设成必填会让"没有归属"这种状态
    # **无法表示**，而它确实存在。None 的语义写死为"没有归属 → 谁也不给看"
    # （`app/api/ownership.py:require_owned_workflow`），而不是"公共可见"。
    #
    # 为什么 username 也存一份：归档要落 `owner_username` 供前端展示，
    # 而工作流跑在后台线程里，那时再去查账号库要多一条连接、还要处理"账号被删了"。
    # 它是**展示用的快照**，判定归属一律用 owner_user_id（用户名可以改，id 不会）。
    owner_user_id: int | None = Field(
        default=None, description="发起者的用户 id；None 表示没有归属，谁都不给看"
    )
    owner_username: str | None = Field(
        default=None, description="发起者的用户名（展示用快照，不参与归属判定）"
    )

    status: WorkflowStatus = Field(default=WorkflowStatus.PENDING)
    step: WorkflowStep = Field(default="profiling", description="当前阶段名，前端查中文文案")
    # step_index 是"当前阶段在 6 步里的下标（0 起）"。阶段是**顺序进入**的：
    # `orchestrator` 先 `_enter(EXECUTING, "execution")`，执行完才 `_enter(CHARTING,
    # "charting")` —— 所以进入某个阶段时，它前面的阶段都已经走完，
    # `step_index` 恰好等于"已完成阶段数"（`_enter` 里就是 `_STEPS.index(step)`）。
    # 换句话说 `step_index / total_steps` 就是完成比例；前端画进度条时用它没问题。
    # 唯一要避免的读法是"已经走完几步 = step_index + 1"（那会把**正在进行**的阶段
    # 也算成完成的）。工具任务（T1/T2…）的完成情况看 `results`，与这里无关。
    step_index: int = Field(default=0, ge=0, description="当前阶段下标（0 起），前端进度条用")
    total_steps: int = Field(default=6, ge=1)

    # 各阶段的产物。默认全是 None / 空列表 —— "还没到那一步"和"那一步失败了"
    # 是两件事，前者是 None，后者会体现在 status 与 error 上。
    profile: DatasetProfile | None = Field(default=None)
    plan: TaskPlan | None = Field(default=None)
    results: list[ToolResult] = Field(default_factory=list, description="按 task_id 顺序")
    charts: list[ChartResult] = Field(default_factory=list)
    report: FinalReport | None = Field(default=None)
    validation: ValidationResult | None = Field(default=None)

    # has_report 是 report 的**派生字段**，为什么还要单独存一份？
    #   因为接口 6（状态查询）故意不返回 report（它可能很大），
    #   前端需要一个廉价的布尔量来决定"要不要去调报告接口"。
    #   规则写死：has_report = (report is not None)，见架构文档 §7.1 第 3 条。
    has_report: bool = Field(default=False, description="是否已有报告可取；由 report 是否为 None 决定")

    # 限额与计数器：防止"模型 + 重试"组合成死循环（架构文档 §5.3）。
    retry_count: int = Field(default=0, ge=0, le=6, description="整条工作流的重试总次数，上限 6")
    max_steps: int = Field(default=6, ge=1, le=8, description="计划里最多几个任务")
    tool_call_count: int = Field(default=0, ge=0, description="已执行的工具调用总数（含重试）")

    error: ApiError | None = Field(default=None, description="顶层失败原因，复用 app/schemas.py 的 ApiError")

    created_at: datetime = Field(description="创建时间（UTC）")
    started_at: datetime | None = Field(default=None, description="后台线程真正开始的时间")
    finished_at: datetime | None = Field(default=None, description="结束时间")
    heartbeat_at: datetime | None = Field(
        default=None,
        description="每推进一步刷新；轮询时发现太久没刷新就判定超时并落 FAILED",
    )


class QuestionRequest(BaseModel):
    """POST /api/analysis 的请求体。"""

    dataset_id: str = Field(min_length=1, description="针对哪个数据集")
    question: str = Field(min_length=2, max_length=500, description="中文问题，2–500 字")
    max_steps: int = Field(
        default=6, ge=1, le=8, description="最多规划几个任务；给换个更复杂的计划留的旋钮"
    )


class AnalysisAccepted(BaseModel):
    """`POST /api/analysis` 的返回体（HTTP 202）。

    为什么需要一个单独的模型，而不是直接回 `WorkflowStatusData` 或 `WorkflowState`：
        - 接口 5 的语义是"**已受理**"，此刻后台线程可能还没启动。回一个完整状态会
          让前端以为"已经开始跑了"，而实际 `step` 可能还停在初始值；
        - 它只要 4 个字段，前端拿到 `workflow_id` 就去轮询接口 6 了，其余都是白传。

    为什么带 `status`：受理时的状态**恒为 `PENDING`**。写进返回体是为了让前端不必
    假设这件事（将来若真的加了任务队列，这里会变成 `QUEUED`，前端代码不用改）。

    架构文档 §8.2 的示例代码一直在用 `ApiResponse[AnalysisAccepted]`，但直到 D4
    冻结契约时这个模型才落地 —— "引用先于定义出现"正是 §14.5 第 4 条记下的教训。
    """

    workflow_id: str = Field(pattern=r"^[0-9a-f]{32}$", description="uuid4().hex，轮询接口的键")
    dataset_id: str = Field(min_length=1, description="原样回显，便于前端核对没串数据集")
    status: WorkflowStatus = Field(
        default=WorkflowStatus.PENDING, description="受理时的状态，恒为 PENDING"
    )
    created_at: datetime = Field(description="受理时间；前端拿它和轮询到的 finished_at 算总耗时")

    @classmethod
    def from_state(cls, state: WorkflowState) -> "AnalysisAccepted":
        """从刚登记的状态里抽出受理回执。

        和 `WorkflowStatusData.from_state()` 同一个理由：接口只抄它需要的字段，
        将来给 `WorkflowState` 加内部字段时，不会莫名其妙出现在响应体里。
        """
        return cls(
            workflow_id=state.workflow_id,
            dataset_id=state.dataset_id,
            status=state.status,
            created_at=state.created_at,
        )


class WorkflowStatusData(BaseModel):
    """GET /api/workflows/{id} 的返回体：**故意不含 report / results**。

    为什么单独定义一个模型，而不是直接返回 WorkflowState：
        ① 状态接口 1 秒被调一次，必须保持很小（< 2 KB）；`results` 里的表格
           可能有几百行，`report` 更长，塞进来等于每秒白传几十 KB；
        ② 接口契约要稳定 —— 直接返回 WorkflowState 的话，将来给 state 加一个
           内部字段会立刻出现在 API 里，前端莫名其妙多收到一个东西。

    `tasks` 里只给前端要显示的四项（task_id / description / status / error），
    不给 params —— 用户不需要看见工具参数，那是排查时才看的东西。
    """

    workflow_id: str
    dataset_id: str
    question: str
    status: WorkflowStatus
    step: WorkflowStep
    step_index: int
    total_steps: int
    tasks: list["TaskBrief"] = Field(default_factory=list)
    charts: list[ChartResult] = Field(default_factory=list)
    error: ApiError | None = None
    created_at: datetime
    finished_at: datetime | None = None
    has_report: bool = False

    @classmethod
    def from_state(cls, state: WorkflowState) -> "WorkflowStatusData":
        """从完整状态里抽出"前端轮询需要的那一小部分"。

        这里唯一需要动脑的地方是 `has_report`：**不抄 `state.has_report`，
        而是现场算 `state.report is not None`**。

        为什么：`has_report` 是派生值，派生值就有一个"忘了同步"的风险
        （某个分支只设了 report、忘了改 has_report，前端就会一直不去取报告）。
        在这里现算一次，接口的正确性就不再依赖"每个写 report 的地方都记得同步"。
        代价是每次多算一次 `is not None`，可以忽略。
        """
        return cls(
            workflow_id=state.workflow_id,
            dataset_id=state.dataset_id,
            question=state.question,
            status=state.status,
            step=state.step,
            step_index=state.step_index,
            total_steps=state.total_steps,
            tasks=[TaskBrief.from_task(task) for task in (state.plan.tasks if state.plan else [])],
            charts=state.charts,
            error=state.error,
            created_at=state.created_at,
            finished_at=state.finished_at,
            has_report=state.report is not None,
        )


class TaskBrief(BaseModel):
    """任务在前端状态列表里的精简形态。"""

    task_id: str
    description: str
    status: TaskStatus
    error: str | None = None

    @classmethod
    def from_task(cls, task: AnalysisTask) -> "TaskBrief":
        return cls(
            task_id=task.task_id,
            description=task.description,
            status=task.status,
            error=task.error,
        )


# WorkflowStatusData.tasks 用的是字符串前向引用，Pydantic v2 需要显式重建一次
# 才能把 TaskBrief 解析进去（TaskBrief 定义在它下面）。放在文件末尾是刻意的：
# 这样读者从上往下读时，先看到"状态接口返回什么"，再看到"里面的任务是什么"。
WorkflowStatusData.model_rebuild()
