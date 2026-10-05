"""统一错误结构、稳定错误码与异常归一化。

设计原则：
- 对调用方暴露的只有 ``code``（稳定字符串错误码）与 ``message``（可读提示），
  以及可选的字段级 ``details``；绝不透出底层异常类型或堆栈。
- HTTP 状态码由错误码决定，保证不同版本下同一类问题落到同一结构、同一码值。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class ErrorCode:
    """稳定错误码常量。

    码值一旦随版本发布即成为契约的一部分，只允许新增，不允许改动语义。
    """

    # 版本生命周期
    VERSION_RETIRED = "VERSION_RETIRED"
    VERSION_UNKNOWN = "VERSION_UNKNOWN"
    # 归一化的请求/业务错误
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: 错误码 -> 默认 HTTP 状态码
DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.VERSION_RETIRED: 410,
    ErrorCode.VERSION_UNKNOWN: 404,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: 错误码 -> 面向调用方的稳定中文提示（不含任何内部实现细节）
DEFAULT_MESSAGES: dict[str, str] = {
    ErrorCode.VERSION_RETIRED: "请求的接口版本已下线，请升级到推荐版本。",
    ErrorCode.VERSION_UNKNOWN: "请求的接口版本不存在。",
    ErrorCode.VALIDATION_ERROR: "请求参数校验失败。",
    ErrorCode.BAD_REQUEST: "请求参数有误。",
    ErrorCode.RESOURCE_NOT_FOUND: "目标资源不存在。",
    ErrorCode.CONFLICT: "请求与当前资源状态冲突。",
    ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试。",
    ErrorCode.INTERNAL_ERROR: "服务内部错误，请稍后重试。",
}


class APIError(Exception):
    """业务异常基类：抛出后由统一异常处理器归一化为错误信封。

    子类只需覆盖类属性 ``code``；也允许在抛出时显式覆盖 message/details。
    """

    code: str = ErrorCode.INTERNAL_ERROR
    status_code: int = DEFAULT_STATUS[ErrorCode.INTERNAL_ERROR]
    message: str = DEFAULT_MESSAGES[ErrorCode.INTERNAL_ERROR]

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
    ) -> None:
        self.code = code or type(self).code
        self.status_code = status_code or DEFAULT_STATUS[self.code]
        self.message = message or DEFAULT_MESSAGES[self.code]
        self.details = details
        super().__init__(self.message)

    def to_body(self) -> dict[str, Any]:
        """转换为统一错误响应体。"""
        return error_body(self.code, self.message, self.details)


class BadRequestError(APIError):
    code = ErrorCode.BAD_REQUEST


class ResourceNotFoundError(APIError):
    code = ErrorCode.RESOURCE_NOT_FOUND


class ConflictError(APIError):
    code = ErrorCode.CONFLICT


def error_body(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    """构造统一错误响应体::

        {"error": {"code": "...", "message": "...", "details": [...]}}

    ``details`` 为空时不出现在响应里，保证结构稳定且无多余字段。
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}


def normalize_field_errors(errors: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """把底层校验错误列表归一化为稳定的字段级明细。

    只保留字段位置与问题描述，丢弃一切可能暴露内部实现的上下文
    （如输入原始对象、异常上下文等）。
    """
    result: list[dict[str, str]] = []
    for item in errors:
        location = item.get("loc") or ()
        field = ".".join(str(part) for part in location if part != "body") or "(root)"
        result.append({"field": field, "issue": str(item.get("msg", "校验失败"))})
    result.sort(key=lambda x: (x["field"], x["issue"]))
    return result
