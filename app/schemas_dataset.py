"""数据集相关的 Pydantic 模型：上传、预览、画像。

为什么单独一个文件，而不是都塞进 app/schemas.py？
    app/schemas.py 放的是"跨模块通用的外壳"（ApiResponse / ApiError / ErrorCode）。
    这个文件放的是"数据集这一块专用的结构"。分开以后，
    app/data/loader.py 与 app/data/profiler.py 只需要 import 这个文件，
    不会被无关的接口外壳牵扯。

一条贯穿全项目的铁律（架构文档 §4）：
    **pd.DataFrame 绝不进入任何 Pydantic 模型。**
    DataFrame 是可变的、可能很大的对象。Pydantic 校验时会尝试复制它，
    既慢又容易在序列化时报 `is not JSON serializable`。
    所以模型里只出现 `dataset_id: str` 这种小字符串，
    需要真正的 DataFrame 时用 DatasetStore 现取。

面试常问："你的数据怎么在模块间传递？"
    答：DataFrame 放在进程内的 DatasetStore 里，用 dataset_id 当钥匙；
    跨模块、跨 HTTP 传的都是 id 和纯 JSON 的元信息。
    好处是响应永远不会因为 NaN / numpy 标量 / Timestamp 而序列化失败
    （这些都被 preview_rows / 画像字段显式转成了 str 或 float）。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# 允许上传的扩展名。注意：**没有 xls**。
# 原因：老的 .xls 是二进制专有格式，pandas 需要 xlrd（已停止维护），
# 而且 .xls 文件里宏/公式的处理更容易出意外。直接在入口拒绝，比读到一半报错好。
FileExt = Literal["csv", "xlsx"]

# 我们对一列的"语义类型"判断结果（不是 pandas 的 dtype，而是业务视角）。
# 两者都要保留：planner 与工具用 semantic_type 决定"这列能不能求和"，
# 而 dtype 是排错时的原始依据。
SemanticType = Literal["numeric", "categorical", "datetime", "boolean", "text"]


class DatasetSummary(BaseModel):
    """数据集精简版：POST /api/datasets/upload 的返回体。

    为什么上传只返回精简版、不直接返回完整的 Dataset？
        前 5 行预览 + 列名 + dtype 加起来可能几 KB，
        而上传响应应该尽快返回给用户。完整信息走 GET /api/datasets/{id}。
    """

    dataset_id: str = Field(description="uuid4().hex，32 位小写十六进制")
    original_filename: str = Field(description="用户上传时的原始文件名，只用于展示")
    row_count: int = Field(ge=0, description="行数（不含表头）")
    column_count: int = Field(ge=0, description="列数")
    created_at: datetime = Field(description="上传时间（UTC）")
    has_profile: bool = Field(default=False, description="是否已生成数据画像")


class Dataset(DatasetSummary):
    """数据集完整版：GET /api/datasets/{id} 的返回体（比 Summary 多文件与预览信息）。

    这里用**继承**而不是重新写一遍字段：
    Dataset "就是" 一个 Summary 再加几个字段。子类自动拥有父类的全部字段，
    以后往 DatasetSummary 加字段，Dataset 会一起拿到，不会漏。
    （Pydantic 的子类校验规则和父类一致，不需要额外配置。）
    """

    stored_filename: str = Field(description="磁盘上的真实文件名，与用户输入完全无关")
    file_ext: FileExt = Field(description="白名单扩展名，csv 或 xlsx")
    size_bytes: int = Field(ge=0, description="文件字节数")
    sha256: str = Field(description="文件内容的 sha256，用于判断是否是同一个文件")
    columns: list[str] = Field(default_factory=list, description="列名列表，保持原始顺序")
    dtypes: dict[str, str] = Field(
        default_factory=dict, description="列名 → pandas dtype 字符串，例 {'销售额': 'float64'}"
    )
    preview_rows: list[dict[str, str]] = Field(
        default_factory=list,
        description="前 5 行；**所有值统一转成 str**，避免 NaN / Timestamp 无法序列化",
    )


class ColumnProfile(BaseModel):
    """单列的画像。全部字段都由 pandas 真实计算，任何 AI 都不参与。"""

    name: str = Field(description="列名")
    dtype: str = Field(description="pandas dtype 字符串，例 'int64' / 'object'")
    semantic_type: SemanticType = Field(
        description="我们的语义判断：numeric/categorical/datetime/boolean/text"
    )
    null_count: int = Field(ge=0, description="缺失值个数")
    null_ratio: float = Field(ge=0.0, le=1.0, description="缺失比例，保留 4 位小数")
    unique_count: int = Field(ge=0, description="去重后的取值个数")

    # 下面这些只对部分类型有意义，所以都是可选（默认 None）。
    # 例如文本列没有 mean_value，日期列没有 q1_value。
    min_value: float | str | None = Field(default=None, description="数值列最小值；日期列放 ISO 字符串")
    max_value: float | str | None = Field(default=None, description="同上，最大值")
    mean_value: float | None = Field(default=None, description="仅数值列：平均值")
    std_value: float | None = Field(default=None, description="仅数值列：标准差（ddof=1，样本标准差）")
    median_value: float | None = Field(default=None, description="仅数值列：中位数")
    q1_value: float | None = Field(default=None, description="仅数值列：25% 分位数")
    q3_value: float | None = Field(default=None, description="仅数值列：75% 分位数")
    top_values: list[dict[str, Any]] = Field(
        default_factory=list,
        description="文本/分类列的最高频前 3 个值，形如 [{'value':'华东','count':42}]",
    )
    sample_values: list[str] = Field(default_factory=list, description="前 3 个非空样例值（已转 str）")


class DatasetProfile(BaseModel):
    """整份数据的画像：GET /api/datasets/{id}/profile 的返回体。

    这是"Data Profiling Agent"对外的唯一产物，也是 planner 制定分析计划时
    看到的全部数据信息 —— 模型看不到真实数据，只能看到这里。
    """

    dataset_id: str
    row_count: int = Field(ge=0, description="总行数")
    column_count: int = Field(ge=0, description="总列数")
    columns: list[ColumnProfile] = Field(default_factory=list, description="逐列画像")
    duplicate_row_count: int = Field(ge=0, description="完全重复的行数（整行所有列都相同）")
    memory_bytes: int = Field(ge=0, description="df.memory_usage(deep=True).sum()，真实内存占用")
    warnings: list[str] = Field(
        default_factory=list,
        description="面向用户的中文提示，例 '列 销售额 缺失 12.30%，相关汇总可能偏低'",
    )
    built_at: datetime = Field(description="画像生成时间（UTC）")
