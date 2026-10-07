"""并发/门禁测试场景使用的具体模型类（FastAPI 需要可解析的具体注解）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ReqV1(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    nickname: str | None = None


class RespV1(BaseModel):
    id: int
    name: str
    nickname: str | None = None


class ReqV1Compat(BaseModel):
    """兼容演进：新增可选字段 bio。"""

    name: str = Field(min_length=1, max_length=50)
    nickname: str | None = None
    bio: str | None = None


class ReqV1Breaking(BaseModel):
    """破坏演进：删除请求字段 nickname。"""

    name: str = Field(min_length=1, max_length=50)


class RespV1Breaking(BaseModel):
    """破坏演进：响应删除字段 nickname。"""

    id: int
    name: str
