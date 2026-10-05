"""自描述契约文档生成。

- 从 FastAPI 路由与 Pydantic 模型自动提取每个版本的端点与模型结构；
- 合并版本注册表中的生命周期元数据（废弃状态、计划下线时间、废弃字段）；
- 附带统一错误结构说明；
- 输出完全确定性：所有集合排序、JSON 序列化使用 sort_keys，
  不嵌入时间戳等易变数据，多次生成结果逐字节一致；
- 快照按版本分文件落盘（contracts/contract-<version>.json），
  已存在的快照若内容变化会拒绝覆盖，保护历史版本契约。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.errors import ErrorCode, ErrorEnvelope
from app.versioning import VersionInfo, VersionRegistry


def _version_of(route: APIRoute) -> str | None:
    for part in route.path.strip("/").split("/"):
        if part.startswith("v") and part[1:].isdigit():
            return part
    return None


def _schema_of(model: Any) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_json_schema()


def _version_block(info: VersionInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "status": info.status.value,
        "deprecated_since": info.deprecated_since.isoformat() if info.deprecated_since else None,
        "sunset_at": info.sunset_at.isoformat() if info.sunset_at else None,
        "successor": info.successor,
        "deprecated_fields": {
            field: day.isoformat() for field, day in sorted(info.deprecated_fields.items())
        },
    }


def _iter_api_routes(app: FastAPI) -> list[APIRoute]:
    """展开应用路由；兼容惰性挂载的 _IncludedRouter。"""
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            routes.extend(r for r in original.routes if isinstance(r, APIRoute))
    return routes


def generate_contract(app: FastAPI, registry: VersionRegistry) -> dict[str, Any]:
    """生成全量契约文档（确定性输出）。"""
    endpoints: list[dict[str, Any]] = []
    for route in _iter_api_routes(app):
        if not route.include_in_schema:
            continue
        version = _version_of(route)
        if version is None:
            continue
        request_model = (
            getattr(route.body_field.field_info, "annotation", None)
            if route.body_field
            else None
        )
        endpoints.append(
            {
                "version": version,
                "method": sorted(route.methods)[0],
                "path": route.path,
                "summary": route.summary or route.name,
                "request": _schema_of(request_model),
                "response": _schema_of(route.response_model),
            }
        )
    endpoints.sort(key=lambda e: (e["version"], e["path"], e["method"]))

    return {
        "versions": [_version_block(i) for i in registry.all()],
        "endpoints": endpoints,
        "errors": {
            "envelope": ErrorEnvelope.model_json_schema(),
            "codes": sorted(c.value for c in ErrorCode),
        },
    }


def contract_for_version(contract: dict[str, Any], version: str) -> dict[str, Any]:
    """从全量契约中切出单个版本的契约片段。"""
    return {
        "version": next(v for v in contract["versions"] if v["name"] == version),
        "endpoints": [e for e in contract["endpoints"] if e["version"] == version],
        "errors": contract["errors"],
    }


def render_json(data: dict[str, Any]) -> str:
    """确定性 JSON 序列化：排序键 + 固定缩进 + 末尾换行。"""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class SnapshotConflictError(Exception):
    """已存在的历史快照与新生成内容不一致，拒绝覆盖。"""


def write_snapshots(
    app: FastAPI,
    registry: VersionRegistry,
    directory: Path,
    *,
    allow_update: bool = False,
) -> list[Path]:
    """按版本写快照文件。已存在且内容不同的快照默认拒绝覆盖。"""
    contract = generate_contract(app, registry)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for version in [v.name for v in registry.all()]:
        path = directory / f"contract-{version}.json"
        content = render_json(contract_for_version(contract, version))
        if path.exists() and path.read_text(encoding="utf-8") != content:
            if not allow_update:
                raise SnapshotConflictError(
                    f"snapshot {path} differs from generated contract; "
                    "refusing to overwrite history (use allow_update to bump intentionally)"
                )
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
