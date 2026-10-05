"""v2 接口：当前推荐版本。

演进点（相对 v1）：
- ``name`` 拆成 ``first_name`` / ``last_name``；
- 新增 ``tier`` 字段；
- ``email`` 在 v2 中被标记为字段级废弃，计划下线，文档与响应头都会声明；
- POST 入参同时接受 v1 风格的 ``name``（兼容期内容忍），但会归一化拆分；
- 另保留一个接口级废弃示例 ``/v2/reports/legacy-summary``。
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, model_validator

from app import store
from app.errors import BadRequestError, ResourceNotFoundError

router = APIRouter(prefix="/v2", tags=["users-v2"])


class UserV2(BaseModel):
    """v2 用户视图。"""

    id: int = Field(..., description="用户 ID。")
    first_name: str = Field(..., description="名。")
    last_name: str = Field(..., description="姓。")
    tier: str = Field(..., description="会员等级：free / pro / enterprise。")
    email: str | None = Field(
        default=None,
        deprecated=True,  # Pydantic 2.13 支持，OpenAPI 3.1 会生成 deprecated 标记
        description="[已废弃] 邮箱字段，计划在 v3 移除；请改用联系方式接口。",
        json_schema_extra={"sunset": "2027-10-01"},
    )


class CreateUserV2Request(BaseModel):
    """创建用户请求（v2）。

    兼容期内同时接受拆分后的 first_name/last_name 与 v1 风格的 name；
    两者必须且只能提供一种。
    """

    first_name: str | None = Field(default=None, min_length=1, max_length=50)
    last_name: str | None = Field(default=None, min_length=1, max_length=50)
    name: str | None = Field(
        default=None,
        deprecated=True,
        description="[已废弃] v1 风格完整姓名，仅兼容期可用；请改用 first_name/last_name。",
    )
    email: str | None = Field(default=None, deprecated=True)
    tier: str = Field(default="free", pattern="^(free|pro|enterprise)$")

    @model_validator(mode="after")
    def _normalize_name(self) -> CreateUserV2Request:
        split_provided = self.first_name is not None and self.last_name is not None
        if not split_provided and not self.name:
            raise BadRequestError(
                "必须提供 first_name 与 last_name（或兼容字段 name）。",
            )
        if split_provided and self.name:
            raise BadRequestError("name 与 first_name/last_name 不能同时提供。")
        if not split_provided and self.name:
            parts = self.name.strip().split(" ", 1)
            self.first_name = parts[0]
            self.last_name = parts[1] if len(parts) > 1 else ""
        return self


def _to_v2(row: dict[str, object]) -> UserV2:
    name = str(row["name"])
    first, _, last = name.partition(" ")
    return UserV2(
        id=int(row["id"]),
        first_name=first,
        last_name=last,
        tier=str(row.get("tier", "free")),
        email=row.get("email") if isinstance(row.get("email"), str) else None,
    )


@router.get("/users", response_model=list[UserV2], summary="查询用户列表（v2）")
def list_users(response: Response) -> list[UserV2]:
    # 字段级废弃：即使整体版本仍 active，也对调用方做可机器识别的提示
    response.headers["Sunset"] = 'Thu, 30 Sep 2027 00:00:00 GMT'
    response.headers["Link"] = '</v3>; rel="successor-version"'
    response.headers["Deprecation"] = 'fields: email'
    return [_to_v2(row) for row in store.list_users()]


@router.get("/users/{user_id}", response_model=UserV2, summary="按 ID 查询用户（v2）")
def get_user(user_id: int, response: Response) -> UserV2:
    row = store.get_user(user_id)
    if row is None:
        raise ResourceNotFoundError(f"用户 {user_id} 不存在。")
    response.headers["Deprecation"] = 'fields: email'
    return _to_v2(row)


@router.post("/users", response_model=UserV2, status_code=201, summary="创建用户（v2）")
def create_user(payload: CreateUserV2Request, response: Response) -> UserV2:
    row = store.create_user(
        name=f"{payload.first_name} {payload.last_name}".strip(),
        email=payload.email,
        tier=payload.tier,
    )
    if payload.name is not None:
        # 使用了废弃入参：成功但带 Sunset 提示，引导新调用方迁移
        response.headers["Deprecation"] = 'request-fields: name'
        response.headers["Link"] = '</v2/users>; rel="deprecation"'
    return _to_v2(row)


@router.get(
    "/reports/legacy-summary",
    summary="[已废弃] 旧版用户汇总报表，计划 2027-04-01 下线",
    deprecated=True,
    tags=["reports-v2-deprecated"],
)
def legacy_summary() -> dict[str, object]:
    """接口级废弃示例：端点整体保留但已宣告下线计划。"""
    users = store.list_users()
    if not users:
        # 业务规则失败同样走统一信封
        raise BadRequestError("当前没有可汇总的用户数据。")
    tiers: dict[str, int] = {}
    for row in users:
        tiers[str(row.get("tier", "free"))] = tiers.get(str(row.get("tier", "free")), 0) + 1
    return {"total": len(users), "by_tier": tiers}


def install(app: object) -> None:
    """把 v2 路由安装到应用。"""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    app.include_router(router)
