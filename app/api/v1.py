"""v1 接口：已废弃（deprecated）但仍在线，响应字段形状冻结在 v1 契约。

变更纪律：本模块不允许再新增必填字段或改变既有字段语义；
新需求通过 v2 承载。废弃信息由版本注册表与中间件统一对外声明。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import store
from app.errors import ResourceNotFoundError

router = APIRouter(prefix="/v1", tags=["users-v1"])


class UserV1(BaseModel):
    """v1 用户视图：单一 name 字段，不含 tier（v2 才引入）。"""

    id: int = Field(..., description="用户 ID。")
    name: str = Field(..., description="姓名（单字段，v2 起拆分为 first_name/last_name）。")
    email: str | None = Field(default=None, description="邮箱。")


class CreateUserV1Request(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="完整姓名。")
    email: str | None = Field(default=None, description="邮箱，可选。")


@router.get(
    "/users",
    response_model=list[UserV1],
    summary="[v1] 查询用户列表（已废弃）",
)
def list_users() -> list[UserV1]:
    return [UserV1(**row) for row in store.list_users()]


@router.get(
    "/users/{user_id}",
    response_model=UserV1,
    summary="[v1] 按 ID 查询用户（已废弃）",
)
def get_user(user_id: int) -> UserV1:
    row = store.get_user(user_id)
    if row is None:
        # 业务异常，由统一处理器归一化为 RESOURCE_NOT_FOUND 信封
        raise ResourceNotFoundError(f"用户 {user_id} 不存在。")
    return UserV1(**row)


@router.post(
    "/users",
    response_model=UserV1,
    status_code=201,
    summary="[v1] 创建用户（已废弃）",
)
def create_user(payload: CreateUserV1Request) -> UserV1:
    row = store.create_user(name=payload.name, email=payload.email)
    return UserV1(**row)


def install(app: object) -> None:
    """把 v1 路由安装到应用。"""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    app.include_router(router)
