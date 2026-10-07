"""版本门禁中间件。

对 /vN/... 形式的请求：
- 未知版本 -> 400 VERSION_UNKNOWN（可区分于 404 路由不存在）
- 已下线版本 -> 410 VERSION_SUNSET（一致的拒绝结构）
- 已废弃版本 -> 放行，但响应带 Deprecation / Sunset / Link 提示头；
  若本次请求/响应实际用到了已废弃字段，再额外带结构化的 Deprecation-Notice
  响应头，逐字段说明下线计划与迁移建议（活跃版本不产生该提醒）。

注意：中间件层抛出的异常不会经过 FastAPI 的路由级异常处理器，
因此这里直接构造与全局异常归一化一致的 ErrorEnvelope 响应。
"""
from __future__ import annotations

import json
import re

from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.errors import ErrorCode, _envelope
from app.versioning import FieldDeprecation, VersionRegistry, VersionStatus

_VERSION_RE = re.compile(r"^/(v\d+)(/|$)")

DEPRECATION_NOTICE_HEADER = "Deprecation-Notice"


def _top_level_keys(value: object) -> set[str]:
    """只看顶层键：调用方“传了哪个字段”对应 JSON 顶层属性。"""
    return set(value) if isinstance(value, dict) else set()  # type: ignore[arg-type]


def _populated_top_level_keys(value: object) -> set[str]:
    """响应里带出了非空实际值的顶层字段；显式 null 不算“还在用”。"""
    if not isinstance(value, dict):
        return set()
    return {key for key, val in value.items() if val is not None}


def _build_notice_entries(
    used_fields: set[str],
    direction: str,
    info: object,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for field in sorted(used_fields):
        deprecation: FieldDeprecation | None = info.field_deprecation(field)  # type: ignore[attr-defined]
        if deprecation is None:
            continue
        entries.append(
            {
                "field": field,
                "direction": direction,
                "deprecated_since": deprecation.deprecated_since.isoformat(),
                "sunset_at": deprecation.sunset_at.isoformat() if deprecation.sunset_at else None,
                "replacement": deprecation.replacement,
                "reason": deprecation.reason,
            }
        )
    return entries


class VersionGateMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, registry: VersionRegistry) -> None:  # noqa: ANN001
        super().__init__(app)
        self.registry = registry

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        match = _VERSION_RE.match(request.url.path)
        if match:
            version = match.group(1)
            info = self.registry.get(version)
            if info is None:
                return JSONResponse(
                    status_code=400,
                    content=_envelope(
                        ErrorCode.VERSION_UNKNOWN,
                        f"unknown api version: {version}",
                        api_version=version,
                    ),
                )
            if self.registry.is_sunset(version):
                details = []
                if info.sunset_at:
                    details.append({"field": "sunset_at", "value": info.sunset_at.isoformat()})
                if info.successor:
                    details.append({"field": "successor", "value": info.successor})
                return JSONResponse(
                    status_code=410,
                    content=_envelope(
                        ErrorCode.VERSION_SUNSET,
                        f"api version {version} has been sunset",
                        details,
                        api_version=version,
                    ),
                )

            # 读取请求体用于识别调用方是否传了废弃字段；_CachedRequest 会缓存，
            # 下游路由仍能正常拿到完整请求体。
            request_deprecated: set[str] = set()
            if info.status is VersionStatus.DEPRECATED and info.deprecated_fields:
                try:
                    raw = await request.body()
                    parsed = json.loads(raw) if raw else None
                    request_deprecated = _top_level_keys(parsed) & set(info.deprecated_fields)
                except (ValueError, UnicodeDecodeError):
                    request_deprecated = set()

            response = await call_next(request)

            if info.status is VersionStatus.DEPRECATED:
                response.headers["Deprecation"] = (
                    info.deprecated_since.isoformat() if info.deprecated_since else "true"
                )
                if info.sunset_at:
                    response.headers["Sunset"] = info.sunset_at.isoformat()
                if info.successor:
                    rest = request.url.path[len(version) + 1:]
                    response.headers["Link"] = (
                        f'</{info.successor}{rest}>; rel="successor-version"'
                    )

                entries = _build_notice_entries(request_deprecated, "request", info)

                # 检查响应里是否带出了废弃字段；只有请求已用到废弃字段、
                # 或需要解析响应时才重建响应体，普通请求保持原流式响应。
                if entries or info.deprecated_fields:
                    chunks: list[bytes] = []
                    async for chunk in response.body_iterator:
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    response_deprecated: set[str] = set()
                    if info.deprecated_fields:
                        try:
                            payload = json.loads(body) if body else None
                            response_deprecated = _populated_top_level_keys(
                                payload
                            ) & set(info.deprecated_fields)
                        except (ValueError, UnicodeDecodeError):
                            response_deprecated = set()
                    entries.extend(
                        _build_notice_entries(response_deprecated, "response", info)
                    )
                    if entries:
                        response.headers[DEPRECATION_NOTICE_HEADER] = json.dumps(
                            entries, ensure_ascii=True
                        )
                    return Response(
                        content=body,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )

            return response
        return await call_next(request)
