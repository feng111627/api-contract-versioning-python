"""版本门禁中间件。

对 /vN/... 形式的请求：
- 未知版本 -> 400 VERSION_UNKNOWN（可区分于 404 路由不存在）
- 已下线版本 -> 410 VERSION_SUNSET（一致的拒绝结构）
- 已废弃版本 -> 放行，但响应带 Deprecation / Sunset / Link 提示头

注意：中间件层抛出的异常不会经过 FastAPI 的路由级异常处理器，
因此这里直接构造与全局异常归一化一致的 ErrorEnvelope 响应。
"""
from __future__ import annotations

import re

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.errors import ErrorCode, _envelope
from app.versioning import VersionRegistry, VersionStatus

_VERSION_RE = re.compile(r"^/(v\d+)(/|$)")


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
            return response
        return await call_next(request)
