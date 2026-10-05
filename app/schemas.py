"""跨版本复用的通用数据模型（统一错误结构等）。

错误信封的形状由这里唯一确定，文档与异常处理器都引用同一模型，
保证“文档里写的错误结构”和“运行时返回的错误结构”一致。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """单条字段级错误明细。"""

    field: str = Field(
        ...,
        description="出错字段的位置，形如 user.email；根级错误为 (root)。",
    )
    issue: str = Field(..., description="该字段的问题描述（人类可读）。")


class ErrorBody(BaseModel):
    """错误信封内层结构。"""

    code: str = Field(..., description="稳定错误码，如 VALIDATION_ERROR。")
    message: str = Field(..., description="面向调用方的错误说明，不包含内部细节。")
    details: list[ErrorDetail] | dict[str, Any] | None = Field(
        default=None,
        description="可选的字段级明细或补充信息；无补充时不出现。",
    )


class ErrorResponse(BaseModel):
    """所有错误响应的统一顶层信封::

        {"error": {"code": "...", "message": "...", "details": [...]}}
    """

    error: ErrorBody
