"""接口版本注册表、版本状态（在线/废弃/下线）与版本路由守卫。

版本通过路径前缀识别（``/v1/...``、``/v2/...``），属于显式版本：
- 未登记的版本前缀（如 ``/v9/...``）→ 404 + ``VERSION_UNKNOWN``，
  与“版本存在但路由不存在”的 404 可区分；
- 已下线版本（retired）→ 410 + ``VERSION_RETIRED``，绝不落入普通 404；
- 已废弃版本（deprecated）正常服务，但响应带 Deprecation/Sunset/Link 头。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from email.utils import formatdate
from enum import Enum
from calendar import timegm
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import ErrorCode, error_body

#: 匹配 /vN 形式的版本前缀
_VERSION_RE = re.compile(r"^/(v\d+)(?:/|$)")


class VersionStatus(str, Enum):
    """版本生命周期状态。"""

    ACTIVE = "active"          # 正常在线
    DEPRECATED = "deprecated"  # 仍在线但已声明废弃，计划下线
    RETIRED = "retired"        # 已下线，一律拒绝访问


@dataclass(frozen=True)
class VersionInfo:
    """一个接口版本的元数据，是对外契约的一部分。"""

    name: str
    status: VersionStatus
    introduced: str = ""
    deprecated_since: str | None = None  # ISO 日期，如 2026-10-06
    sunset: date | None = None           # 计划下线日期
    successor: str | None = None         # 推荐升级到的版本
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为确定性字典（供文档生成，键顺序固定）。"""
        return {
            "name": self.name,
            "status": self.status.value,
            "introduced": self.introduced,
            "deprecated_since": self.deprecated_since,
            "sunset": self.sunset.isoformat() if self.sunset else None,
            "successor": self.successor,
            "notes": self.notes,
        }


class VersionRegistry:
    """版本登记表：集中管理所有版本及其生命周期状态。"""

    def __init__(self, versions: list[VersionInfo] | None = None) -> None:
        self._versions: dict[str, VersionInfo] = {}
        for info in versions or []:
            self.register(info)

    def register(self, info: VersionInfo) -> None:
        if info.name in self._versions:
            raise ValueError(f"版本重复登记: {info.name}")
        self._versions[info.name] = info

    def get(self, name: str) -> VersionInfo | None:
        return self._versions.get(name)

    def all(self) -> list[VersionInfo]:
        """按版本名稳定排序返回全部版本（v2 < v10 的自然语义由调用方命名约束）。"""
        return [self._versions[name] for name in sorted(self._versions)]

    def exists(self, name: str) -> bool:
        return name in self._versions

    def is_retired(self, name: str) -> bool:
        info = self._versions.get(name)
        return info is not None and info.status is VersionStatus.RETIRED


def detect_version(scope: Scope) -> str | None:
    """从请求路径中解析版本名（``/v2/users`` -> ``v2``）。"""
    path = scope.get("path") or ""
    match = _VERSION_RE.match(path)
    return match.group(1) if match else None


def _http_date(value: date) -> str:
    """date -> RFC 7231 IMF-fixdate（Deprecation/Sunset 头使用）。"""
    stamp = timegm(
        (value.year, value.month, value.day, 0, 0, 0, 0, 0, 0)
    )
    return formatdate(stamp, usegmt=True)


class VersionGuardMiddleware:
    """版本守卫：拦截未知版本与已下线版本，给出可区分的拒绝结果。"""

    def __init__(self, app: ASGIApp, versions: VersionRegistry) -> None:
        self.app = app
        self.versions = versions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        name = detect_version(scope)
        if name is not None:
            info = self.versions.get(name)
            if info is None:
                response = JSONResponse(
                    error_body(
                        ErrorCode.VERSION_UNKNOWN,
                        f"接口版本 {name} 不存在。",
                    ),
                    status_code=404,
                )
                await response(scope, receive, send)
                return
            if info.status is VersionStatus.RETIRED:
                details: dict[str, str] | None = None
                if info.successor:
                    details = {"successor": info.successor}
                response = JSONResponse(
                    error_body(
                        ErrorCode.VERSION_RETIRED,
                        f"接口版本 {name} 已于 {info.sunset.isoformat() if info.sunset else '近期'} 下线。",
                        details,
                    ),
                    status_code=410,
                    headers=(
                        {"Link": f'/{info.successor}; rel="successor-version"'}
                        if info.successor
                        else None
                    ),
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class DeprecationHeaderMiddleware:
    """为已废弃版本的响应注入标准废弃提示头。

    - ``Deprecation``: 废弃声明日期（RFC 9745 风格 HTTP-date）
    - ``Sunset``: 计划下线日期（RFC 8594）
    - ``Link``: 后继版本地址（rel="successor-version"）
    - ``X-Api-Version``: 本次命中的版本，便于调用方自查
    """

    def __init__(self, app: ASGIApp, versions: VersionRegistry) -> None:
        self.app = app
        self.versions = versions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        name = detect_version(scope)
        info = self.versions.get(name) if name else None
        extra: list[tuple[bytes, bytes]] = []
        if name:
            extra.append((b"x-api-version", name.encode()))
        if info is not None and info.status is VersionStatus.DEPRECATED:
            if info.deprecated_since:
                dep_date = date.fromisoformat(info.deprecated_since)
                extra.append((b"deprecation", _http_date(dep_date).encode()))
            if info.sunset:
                extra.append((b"sunset", _http_date(info.sunset).encode()))
            if info.successor:
                extra.append(
                    (
                        b"link",
                        f'/{info.successor}; rel="successor-version"'.encode(),
                    )
                )

        if not extra:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + extra
            await send(message)

        await self.app(scope, receive, send_wrapper)
