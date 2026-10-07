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
class FieldDeprecation:
    """单个字段的废弃详情（运行时提醒用；不进入契约快照，避免改写历史）。

    - deprecated_since: 标记废弃的日期。
    - sunset_at: 该字段计划下线/移除的日期（可晚于版本下线日）。
    - replacement: 建议迁移到的字段（可带版本限定，如 "v2.display_name"）。
    - reason: 给调用方的人话说明。
    """

    deprecated_since: date
    sunset_at: date | None = None
    replacement: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class VersionInfo:
    """单个接口版本的元数据。

    - deprecated_since: 标记废弃的日期（仍可用，但响应会带废弃提示）。
    - sunset_at: 计划下线日期；到达该日期后请求一律被拒绝。
    - successor: 建议迁移到的后继版本。
    - deprecated_fields: 字段名 -> 废弃日期（date，仅用于文档与契约快照）
      或完整的 FieldDeprecation（额外携带下线计划与迁移建议，供运行时提醒）。
    """

    name: str
    status: VersionStatus = VersionStatus.ACTIVE
    deprecated_since: date | None = None
    sunset_at: date | None = None
    successor: str | None = None
    deprecated_fields: dict[str, date | FieldDeprecation] = field(default_factory=dict)

    def field_deprecation(self, name: str) -> FieldDeprecation | None:
        """返回字段的完整废弃详情；只登记了日期时自动补全。"""
        entry = self.deprecated_fields.get(name)
        if entry is None:
            return None
        if isinstance(entry, FieldDeprecation):
            return entry
        return FieldDeprecation(deprecated_since=entry)


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
            deprecated_fields={
                "nickname": FieldDeprecation(
                    deprecated_since=date(2026, 1, 15),
                    sunset_at=date(2026, 12, 31),
                    replacement="v2.display_name",
                    reason="nickname 将随 v1 下线移除，请改用 v2 的 display_name",
                )
            },
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
