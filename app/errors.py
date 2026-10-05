"""统一错误结构与异常归一化。

所有对外错误都收敛到 ErrorEnvelope：
- 请求校验失败（FastAPI RequestValidationError）
- 业务异常（BusinessError 及其子类）
- 版本相关拒绝（由中间件直接产出，复用同一 ErrorEnvelope）
- 未捕获的内部异常（不泄露异常类型与堆栈）
"""
from __future__ import annotations

import logging
from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("api.errors")


class ErrorCode(str, Enum):
    """稳定的对外错误码。"""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    VERSION_SUNSET = "VERSION_SUNSET"
    VERSION_UNKNOWN = "VERSION_UNKNOWN"
    BUSINESS_RULE_VIOLATION = "BUSINESS_RULE_VIOLATION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorBody(BaseModel):
    """统一错误负载。"""

    code: ErrorCode
    message: str
    details: list[dict] = []
    api_version: str | None = None


class ErrorEnvelope(BaseModel):
    """统一错误响应外壳。"""

    error: ErrorBody


class BusinessError(Exception):
    """业务异常基类：message 可对外展示，不携带堆栈。"""

    code: ErrorCode = ErrorCode.BUSINESS_RULE_VIOLATION
    http_status: int = 422

    def __init__(self, message: str, details: list[dict] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []


def _envelope(
    code: ErrorCode,
    message: str,
    details: list[dict] | None = None,
    api_version: str | None = None,
) -> dict:
    return ErrorEnvelope(
        error=ErrorBody(
            code=code, message=message, details=details or [], api_version=api_version
        )
    ).model_dump(mode="json")


def _request_version(request: Request) -> str | None:
    """从路径中提取 /vN 版本段，用于错误响应标注。"""
    parts = request.url.path.strip("/").split("/")
    return parts[0] if parts and parts[0].startswith("v") else None


def register_exception_handlers(app: FastAPI) -> None:
    """把各类异常归一化为 ErrorEnvelope。"""

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "loc": [str(p) for p in e.get("loc", [])],
                "msg": e.get("msg", ""),
                "type": e.get("type", ""),
            }
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope(
                ErrorCode.VALIDATION_FAILED,
                "request validation failed",
                details,
                api_version=_request_version(request),
            ),
        )

    @app.exception_handler(BusinessError)
    async def on_business_error(request: Request, exc: BusinessError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_envelope(exc.code, exc.message, exc.details, _request_version(request)),
        )

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # 内部异常只记录日志，对外不透出类型与堆栈。
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content=_envelope(
                ErrorCode.INTERNAL_ERROR,
                "internal server error",
                api_version=_request_version(request),
            ),
        )
