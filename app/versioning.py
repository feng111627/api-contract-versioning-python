"""接口版本注册与生命周期管理。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class VersionStatus(str, Enum):
    """版本生命周期状态。"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"


@dataclass(frozen=True)
class VersionInfo:
    """单个接口版本的元数据。

    - deprecated_since: 标记废弃的日期（仍可用，但响应会带废弃提示）。
    - sunset_at: 计划下线日期；到达该日期后请求一律被拒绝。
    - successor: 建议迁移到的后继版本。
    - deprecated_fields: 字段名 -> 废弃日期，用于文档与响应提示。
    """

    name: str
    status: VersionStatus = VersionStatus.ACTIVE
    deprecated_since: date | None = None
    sunset_at: date | None = None
    successor: str | None = None
    deprecated_fields: dict[str, date] = field(default_factory=dict)


class VersionRegistry:
    """版本注册表：登记版本元数据并回答版本是否可用。"""

    def __init__(self) -> None:
        self._versions: dict[str, VersionInfo] = {}

    def register(self, info: VersionInfo) -> None:
        if info.name in self._versions:
            raise ValueError(f"duplicate version: {info.name}")
        if info.status is VersionStatus.DEPRECATED and info.deprecated_since is None:
            raise ValueError(f"{info.name}: deprecated version must set deprecated_since")
        if info.status is VersionStatus.SUNSET and info.sunset_at is None:
            raise ValueError(f"{info.name}: sunset version must set sunset_at")
        self._versions[info.name] = info

    def get(self, name: str) -> VersionInfo | None:
        return self._versions.get(name)

    def all(self) -> list[VersionInfo]:
        """按版本名排序返回，保证输出确定性。"""
        return [self._versions[k] for k in sorted(self._versions)]

    def is_known(self, name: str) -> bool:
        return name in self._versions

    def is_sunset(self, name: str, today: date | None = None) -> bool:
        """版本已声明 SUNSET，或已声明 DEPRECATED 且当前日期到达 sunset_at。"""
        info = self._versions.get(name)
        if info is None:
            return False
        if info.status is VersionStatus.SUNSET:
            return True
        if info.sunset_at is not None:
            return (today or date.today()) >= info.sunset_at
        return False


def build_default_registry() -> VersionRegistry:
    """构造服务当前使用的版本注册表。"""
    registry = VersionRegistry()
    registry.register(
        VersionInfo(
            name="v1",
            status=VersionStatus.DEPRECATED,
            deprecated_since=date(2026, 1, 15),
            sunset_at=date(2026, 12, 31),
            successor="v2",
            deprecated_fields={"nickname": date(2026, 1, 15)},
        )
    )
    registry.register(VersionInfo(name="v2", status=VersionStatus.ACTIVE))
    registry.register(
        VersionInfo(
            name="v0",
            status=VersionStatus.SUNSET,
            deprecated_since=date(2025, 6, 1),
            sunset_at=date(2026, 1, 1),
            successor="v2",
        )
    )
    return registry
