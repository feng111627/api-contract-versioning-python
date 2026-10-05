"""v2 路由（当前活跃版本）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.v2 import UserCreateV2, UserV2
from app.store import store

router = APIRouter(prefix="/v2", tags=["v2"])


@router.post("/users", response_model=UserV2, status_code=201)
def create_user(payload: UserCreateV2) -> UserV2:
    record = store.create(
        name=payload.name, display_name=payload.display_name, email=payload.email
    )
    return UserV2(
        id=record.id, name=record.name, display_name=record.display_name, email=record.email
    )


@router.get("/users/{user_id}", response_model=UserV2)
def get_user(user_id: int) -> UserV2:
    record = store.get(user_id)
    return UserV2(
        id=record.id, name=record.name, display_name=record.display_name, email=record.email
    )
