"""版本门禁中间件与运行时废弃提醒。

对 /vN/... 形式的请求：
- 未知版本 -> 400 VERSION_UNKNOWN（可区分于 404 路由不存在）
- 已下线版本 -> 410 VERSION_SUNSET（一致的拒绝结构）
- 已废弃版本 -> 放行，但响应带 Deprecation / Sunset / Link 提示头

字段级运行时废弃提醒：
- detect_deprecated_request_fields 作为路由依赖运行（此时 Pydantic 已缓存请求体，
  再次 await request.json() 不会消耗流），把请求侧命中的废弃字段放到 request.state；
- 中间件在响应返回后扫描响应 JSON，与请求侧命中合并，
  仅当本次请求确实用到废弃字段时注入 Deprecation-Info 结构化头；
- 活跃版本没有字段废弃登记，天然不会产生任何提醒。

注意：中间件层抛出的异常不会经过 FastAPI 的路由级异常处理器，
因此这里直接构造与全局异常归一化一致的 ErrorEnvelope 响应。
"""
from __future__ import annotations

import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import deprecation
from app.errors import ErrorCode, _envelope
from app.versioning import VersionInfo, VersionRegistry, VersionStatus

_VERSION_RE = re.compile(r"^/(v\d+)(/|$)")


async def detect_deprecated_request_fields(request: Request) -> None:
    """路由依赖：记录请求体里实际出现的废弃字段（不改变任何业务行为）。"""
    # 注册表在应用启动时挂到 app.state；未登记版本或非 JSON 请求直接跳过。
    registry = getattr(request.app.state, "registry", None)
    info: VersionInfo | None = None
    if registry is not None:
        match = _VERSION_RE.match(request.url.path)
        info = registry.get(match.group(1)) if match else None
    if info is None or not info.resolved_field_deprecations():
        return
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        return
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 校验失败的请求交给错误归一化处理，废弃提醒不抢戏。
        return
    names = {d.field for d in info.resolved_field_deprecations()}
    request.state.deprecated_request_paths = deprecation.paths_by_field(body, names)


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
            return await self._annotate_field_deprecation(request, response, info)
        return await call_next(request)

    async def _annotate_field_deprecation(
        self, request: Request, response: Response, info: VersionInfo
    ) -> Response:
        """响应确实带出（或请求确实传入）废弃字段时，附加结构化废弃说明头。"""
        deprecations = info.resolved_field_deprecations()
        if not deprecations:
            # 活跃版本：永远不产生字段级提醒。
            return response

        request_paths = getattr(request.state, "deprecated_request_paths", None) or {}

        # 只扫描 JSON 响应；读取后需重建响应体（StreamingResponse 的迭代器只能消费一次）。
        content_type = response.headers.get("content-type", "")
        response_paths: dict[str, list[str]] = {}
        body_bytes = b""
        if "application/json" in content_type:
            async for chunk in response.body_iterator:
                body_bytes += chunk  # type: ignore[operator]
            try:
                response_body = json.loads(body_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                response_body = None
            if response_body is not None:
                names = {d.field for d in deprecations}
                response_paths = deprecation.paths_by_field(response_body, names)

        entries = deprecation.build_notice_entries(
            deprecations, request_paths, response_paths
        )
        if not entries:
            # 本次请求没有用到任何废弃字段：不附加提醒。
            if body_bytes:
                return Response(
                    content=body_bytes,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            return response

        rebuilt = Response(
            content=body_bytes if body_bytes else b"",
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )
        rebuilt.headers[deprecation.DEPRECATION_HEADER] = deprecation.encode_header(entries)
        rebuilt.headers[deprecation.DEPRECATION_FIELDS_HEADER] = ",".join(
            sorted({e["field"] for e in entries})
        )
        return rebuilt
