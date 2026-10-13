"""工具层的数据结构：一个工具跑完长什么样、一张图生成完前端要知道什么。

这个文件回答两个问题
--------------------
1. **一次工具调用结束后，结果长什么样？** → `ToolResult`
2. **一张图生成完后，前端需要知道哪些信息才能把它显示出来？** → `ChartResult`

为什么单独一个文件（而不是塞进 `app/schemas.py`）
-------------------------------------------------
和 `app/schemas_dataset.py` 同样的理由：这两个模型被 `app/tools/`（算）与
`app/agents/`（调度、校验）两边共用，是"工具"这一块自成一体的接口。
`app/schemas.py` 只放跨模块通用的外壳（`ApiResponse` / `ApiError` / `ErrorCode`）。

一条铁律（架构文档 §4.0.2）
--------------------------
**`pd.DataFrame` 绝不进入任何 Pydantic 模型。**

工具算完之后必须把结果变成纯 Python 的 `dict` / `list` / `float` / `str`，
再塞进 `ToolResult.data`。原因是 DataFrame 可变、可能很大，而且里面装着
`numpy.float64`、`NaT`、`Timestamp` 这些"看起来像数字、其实不能进 JSON"的东西。
把它们挡在模型外面，接口响应就永远不会因为序列化失败而 500。

注意 `ToolResult.error_code` 与 `app/schemas.py` 的 `ErrorCode` 是**两套码**
----------------------------------------------------------------------------
`ErrorCode` 是枚举，约束的是 **HTTP 状态码**（404 / 413 / 415……），
出现在失败响应的外壳里。

`ToolResult.error_code` 是**任务级**的失败原因：某个工具没跑成功，
但整个工作流仍然可能返回 `SUCCESS_WITH_WARNINGS`（HTTP 200）。
所以它不叫 `ErrorCode`，取值也不一样（`TOOL_NOT_FOUND` 等五个）。

**两组码不要混用**，这条在架构文档 §7.2 结尾专门写过。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# 工具失败的 5 种原因（与架构文档 §6.3 的 5 类校验一一对应）。
# 用 Literal 而不是裸 str：写错一个字母在开发时就被 Pydantic 抓出来，
# 而不是等到前端拿着一个没人认识的码去查表。
ToolErrorCode = Literal[
    "TOOL_NOT_FOUND",  # 校验 1：工具名不在白名单里
    "BAD_PARAMS",  # 校验 2：参数类型/必填/范围不合法
    "COLUMN_NOT_FOUND",  # 校验 3：请求的列在数据集里不存在
    "BAD_OPERATION",  # 校验 4：操作本身不合法（例：对文本列求均值）
    "BAD_RESULT",  # 校验 5：工具返回的结构不对（这是**我们的** bug）
]

# 四种图表类型。planner 只能从这四种里选，别的一律拒绝。
ChartType = Literal["bar", "line", "pie", "scatter"]


class ToolResult(BaseModel):
    """一次工具调用的完整结果。这是"数字是真的"这件事的载体。

    为什么每个字段都要有：报告里每一句话都要能追到某个 `task_id` 的某个数字，
    而那个数字就在 `data` 里。校验 Agent 做的事情就是"把报告里的数字拿回这里找"。
    """

    task_id: str = Field(description="对应 AnalysisTask.task_id，报告引用它做溯源")
    tool_name: str = Field(description="实际执行的工具名（不是模型想要的名字，是真跑的那个）")
    ok: bool = Field(description="工具是否成功跑完并通过了返回结构校验")
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="工具算出来的结果。**必须是纯 JSON 类型**，不能有 numpy 标量 / NaN / Timestamp",
    )
    columns: list[str] = Field(
        default_factory=list,
        description="若 data 是表格形式（{'rows': [...], 'columns': [...]}），把列名冗余到这里方便前端",
    )
    row_count: int = Field(default=0, ge=0, description="返回的数据行数；0 行要记 warning")
    elapsed_ms: int = Field(ge=0, description="工具函数实际耗时（毫秒），用于发现哪个工具很慢")
    error_code: ToolErrorCode | None = Field(
        default=None, description="失败时的机器码（5 选 1）；成功时为 None"
    )
    error_message: str | None = Field(
        default=None, description="失败时的中文说明，直接给前端显示"
    )
    created_at: datetime = Field(description="生成时间（UTC）")


class ChartResult(BaseModel):
    """一张已落盘的图。前端拿它直接渲染，不做任何字符串拼接。

    A-2 拍板的三处一致（架构文档 §4.8）：接口 6、接口 8、前端图表区
    共用这一份定义。最容易出错的地方是"谁来拼 URL" —— 结论是**后端拼**：

        url      = 给前端用的地址（`<img src>` 直接用）
        filename = 给后端用的钥匙（接口 8 只认它）

    两者都在同一个对象里，谁也不用推导谁。下面那个 `model_validator`
    就是把这条约定变成**会报错的代码**，而不是文档里的一句提醒。
    """

    chart_id: str = Field(
        pattern=r"^[0-9a-f]{12}$",
        description="uuid4().hex[:12]，12 位小写十六进制；磁盘文件名由它决定",
    )
    filename: str = Field(description="磁盘文件名，形如 a1b2c3d4e5f6.png，与用户输入无关")
    title: str = Field(description="图标题（中文，已截断到 60 字以内）")
    chart_type: ChartType = Field(description="图型：柱状 / 折线 / 饼图 / 散点")
    url: str = Field(
        description="后端拼好的完整相对路径：/api/workflows/{workflow_id}/charts/{filename}"
    )

    # 下面 5 个字段不属于 A-2 的"5 个必备字段"，但**不能删**：
    # 校验 Agent 靠 source_task_id 验证"这张图不是凭空画的"，
    # 靠 point_count 验证"图里有几根柱子"和结果行数对得上。
    source_task_id: str = Field(description="数据来源任务 id；校验 Agent 靠它证明图有依据")
    x_field: str = Field(description="X 轴来源字段名")
    y_field: str = Field(description="Y 轴来源字段名")
    point_count: int = Field(ge=0, description="图上数据点个数（用于校验图里有几根柱子）")
    created_at: datetime = Field(description="生成时间（UTC）")

    @model_validator(mode="after")
    def _filename_and_url_must_agree(self) -> "ChartResult":
        """把"文件名由 chart_id 决定、url 由 filename 决定"变成会报错的检查。

        为什么值得写：这三者一旦对不上，前端拿到的 `<img src>` 会 404，
        而**图片 404 不会让任何测试变红** —— 浏览器里只是一张破图。
        与其等演示时才发现，不如在构造对象的那一刻就炸。
        """
        expected = f"{self.chart_id}.png"
        if self.filename != expected:
            raise ValueError(f"filename 必须等于 chart_id + '.png'，期望 {expected}，实际 {self.filename}")
        if not self.url.endswith(f"/charts/{self.filename}"):
            raise ValueError(f"url 必须以 /charts/{self.filename} 结尾，实际 {self.url}")
        return self
