"""v1 版本数据模型。

v1 已废弃但仍可用：nickname 字段已废弃，由 v2 的 display_name 取代。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserCreateV1(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    nickname: str | None = Field(
        default=None,
        deprecated=True,
        description="已废弃，将在 v1 下线时移除，请迁移到 v2 的 display_name",
    )


class UserV1(BaseModel):
    id: int
    name: str
    nickname: str | None = Field(default=None, deprecated=True)
