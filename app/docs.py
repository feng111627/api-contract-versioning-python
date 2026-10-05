"""根据已登记的路由与数据模型自动生成确定性、可复现的接口文档。

生成原则：
- 只扫描显式登记的版本与实际注册的路由，不手写接口清单；
- 所有集合排序、键顺序固定，同一份代码多次生成字节级一致；
- 每个版本独立成节，历史版本契约不会被新版本覆盖；
- 统一错误结构作为共享构件在文档中固定描述。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.schemas import ErrorResponse
from app.versioning import VersionRegistry

CONTRACT_PATH = "/docs/contract"

#: 错误码与 HTTP 状态码的稳定对照（来自 errors 模块单一事实源）
from app.errors import DEFAULT_MESSAGES, DEFAULT_STATUS  # noqa: E402


def _schema(model: Any) -> dict[str, Any] | None:
    """取响应/请求的 JSON Schema；无模型时返回 None。"""
    if model is None:
        return None
    generate = getattr(model, "model_json_schema", None)
    if callable(generate):
        return generate()
    return None


def _collect_route_contexts(app: FastAPI) -> list[Any]:
    """递归展平 FastAPI 0.141 的懒加载路由树，收集有效路由上下文。

    ``app.routes`` 中含 ``_IncludedRouter`` 包装，真正的端点要从其
    ``effective_candidates()`` 里取；包装允许嵌套，故递归处理。
    """
    contexts: list[Any] = []

    def visit(entry: Any) -> None:
        if type(entry).__name__ == "_IncludedRouter":
            for candidate in entry.effective_candidates():
                visit(candidate)
            return
        # _EffectiveRouteContext：具备 path/methods/response_model 等属性
        if hasattr(entry, "path") and hasattr(entry, "methods"):
            contexts.append(entry)

    for entry in app.routes:
        visit(entry)
    return contexts


def _endpoint_schema(route: Any) -> dict[str, Any]:
    """提取单条路由的确定性契约描述。"""
    methods = sorted(
        method for method in (route.methods or ()) if method not in {"HEAD", "OPTIONS"}
    )
    request_schema: dict[str, Any] | None = None
    for body_param in route.dependant.body_params:
        # FastAPI 0.141：body_params 元素本身即 ModelField，类型挂在 field_info 上
        annotation = getattr(getattr(body_param, "field_info", None), "annotation", None)
        request_schema = _schema(annotation)
        break

    return {
        "path": route.path,
        "methods": methods,
        "summary": route.summary or route.name,
        "deprecated": bool(route.deprecated),
        "request_schema": request_schema,
        "response_schema": _schema(route.response_model),
    }


def generate_contract(app: FastAPI, versions: VersionRegistry) -> dict[str, Any]:
    """扫描路由与模型，生成版本化的自描述契约。"""
    endpoints_by_version: dict[str, list[dict[str, Any]]] = {}
    known = {info.name for info in versions.all()}
    for route in _collect_route_contexts(app):
        parts = route.path.strip("/").split("/", 1)
        if not parts or parts[0] not in known:
            continue
        endpoints_by_version.setdefault(parts[0], []).append(_endpoint_schema(route))

    version_sections: list[dict[str, Any]] = []
    for info in versions.all():
        endpoints = sorted(
            endpoints_by_version.get(info.name, []),
            key=lambda item: (item["path"], ",".join(item["methods"])),
        )
        section = {
            "version": info.to_dict(),
            "endpoints": endpoints,
        }
        if info.status.value == "retired":
            # 已下线版本没有在线端点，但拒绝策略本身也是契约的一部分
            section["retirement_policy"] = {
                "http_status": 410,
                "error_code": "VERSION_RETIRED",
            }
        version_sections.append(section)

    return {
        "service": {"name": "api-contract-versioning", "title": app.title},
        "shared": {
            "error_response_schema": ErrorResponse.model_json_schema(),
            "error_codes": [
                {
                    "code": code,
                    "http_status": DEFAULT_STATUS[code],
                    "message": DEFAULT_MESSAGES[code],
                }
                for code in sorted(DEFAULT_STATUS)
            ],
            "unknown_version_policy": {
                "http_status": 404,
                "error_code": "VERSION_UNKNOWN",
            },
        },
        "versions": version_sections,
    }


def serialize_contract(contract: dict[str, Any]) -> str:
    """把契约序列化为稳定的 JSON 字符串（排序键、固定缩进、结尾换行）。"""
    return json.dumps(
        contract,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"


def build_docs_routes(app: FastAPI, versions: VersionRegistry) -> None:
    """挂载 /docs/contract 自描述文档端点。"""

    @app.get(CONTRACT_PATH, include_in_schema=False, tags=["docs"])
    def contract() -> dict[str, Any]:
        return generate_contract(app, versions)


def write_contract_file(app: FastAPI, versions: VersionRegistry, path: str) -> None:
    """把契约落盘，供提交快照与复现校验。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        serialize_contract(generate_contract(app, versions)),
        encoding="utf-8",
    )
