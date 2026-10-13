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
    """错误码。前端可以据此决定是提示用户、还是重试。"""

    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
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
