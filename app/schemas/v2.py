"""v2 版本数据模型（当前活跃版本）。"""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class UserCreateV2(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    display_name: str | None = Field(default=None, max_length=50)
    email: EmailStr


class UserV2(BaseModel):
    id: int
    name: str
    display_name: str | None = None
    email: EmailStr
