"""v1 路由（已废弃，保持旧契约可用）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware import detect_deprecated_request_fields
from app.schemas.v1 import UserCreateV1, UserV1
from app.store import store

router = APIRouter(
    prefix="/v1",
    tags=["v1"],
    dependencies=[Depends(detect_deprecated_request_fields)],
)


@router.post("/users", response_model=UserV1, status_code=201)
def create_user(payload: UserCreateV1) -> UserV1:
    record = store.create(name=payload.name, nickname=payload.nickname)
    return UserV1(id=record.id, name=record.name, nickname=record.nickname)


@router.get("/users/{user_id}", response_model=UserV1)
def get_user(user_id: int) -> UserV1:
    record = store.get(user_id)
    return UserV1(id=record.id, name=record.name, nickname=record.nickname)
