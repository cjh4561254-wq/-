"""全局数据结构（Pydantic 模型）。

这一版只定义"响应外壳" ApiResponse。
后面的工作流、任务、工具结果等模型都会继续加在这个文件或各自模块里。

为什么要把响应外壳抽出来？
    前端只需要写一次解析逻辑：
        先看 ok，ok 为 True 就去读 data，为 False 就读 error.message。
    如果每个接口各写一套 JSON，前端会变成一堆 if，而且报错信息格式不统一。
"""

from enum import Enum
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorCode(str, Enum):
    """错误码。前端可以据此决定是提示用户、还是重试。

    注意区分两个层次（这是本项目的核心接口约定之一）：
        HTTP 状态码 —— HTTP 层面发生了什么（404 找不到 / 413 太大 / 415 类型不支持）
        error.code  —— 业务层面发生了什么（NOT_FOUND / FILE_TOO_LARGE / ...）
    两者同时给出、不冲突。错误码到状态码的映射集中在 app/errors.py 的 ERROR_STATUS。
    """

    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"  # HTTP 方法不对（框架抛的 405）
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"  # 依赖不可用（例：没配大模型密钥）
    WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"  # 工作流超时（HTTP 仍是 200，见 errors.py 的说明）
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(BaseModel):
    """失败时的错误信息。message 面向用户，detail 面向开发者排查。"""

    code: ErrorCode
    message: str
    detail: str | None = None


class ApiResponse(BaseModel, Generic[T]):
    """统一响应外壳：{"ok": bool, "data": ..., "error": ...}

    约定：
      ok=True  -> data 有值，error 为 None
      ok=False -> data 为 None，error 有值
    """

    ok: bool = True
    data: T | None = None
    error: ApiError | None = None
