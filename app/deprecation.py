"""运行时字段级废弃提醒。

当一次请求实际"用到了"已废弃字段时（请求体里传了，或响应体里带出了），
生成结构化废弃说明，随响应头 Deprecation-Info 返回：

- 只对登记了字段废弃信息的版本生效，活跃版本永远不产生提醒；
- 说明里讲清楚字段名、方向（request/response）、计划下线日期、建议迁向；
- 输出完全确定性（按字段名与路径排序），同一请求多次结果一致。

本模块只做纯逻辑：从 JSON 结构里定位被用到的废弃字段、组装条目。
HTTP 头的注入由 app.middleware 完成。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from app.versioning import FieldDeprecation

DEPRECATION_HEADER = "Deprecation-Info"
DEPRECATION_FIELDS_HEADER = "Deprecation-Fields"


def find_deprecated_paths(
    body: Any,
    deprecated_names: set[str],
    prefix: str = "$",
    *,
    _depth: int = 0,
) -> list[str]:
    """在已解析的 JSON 结构中找出所有"用到了废弃字段"的 JSONPath 风格位置。

    - 对象：键名命中废弃字段即记录；值继续递归（嵌套结构也能发现）。
    - 数组：逐元素递归，位置带 [i]。
    """
    if _depth > 64:
        return []
    found: list[str] = []
    if isinstance(body, dict):
        for key in sorted(body):
            location = f"{prefix}.{key}"
            if key in deprecated_names:
                found.append(location)
            found.extend(
                find_deprecated_paths(body[key], deprecated_names, location, _depth=_depth + 1)
            )
    elif isinstance(body, list):
        for index, item in enumerate(body):
            found.extend(
                find_deprecated_paths(
                    item, deprecated_names, f"{prefix}[{index}]", _depth=_depth + 1
                )
            )
    return found


def build_notice_entries(
    deprecations: Iterable[FieldDeprecation],
    request_paths: dict[str, list[str]],
    response_paths: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """根据实际命中的位置组装结构化废弃条目（确定性排序）。

    request_paths/response_paths: 字段名 -> 该字段被用到的位置列表。
    同一字段在同一方向上聚合成一条，位置列表排序去重。
    """
    by_name = {d.field: d for d in deprecations}
    entries: list[dict[str, Any]] = []
    for direction, paths_map in (
        ("request", request_paths),
        ("response", response_paths),
    ):
        for field_name in sorted(paths_map):
            dep = by_name.get(field_name)
            if dep is None:
                continue
            locations = sorted(set(paths_map[field_name]))
            if not locations:
                continue
            entries.append(
                {
                    "field": field_name,
                    "direction": direction,
                    "locations": locations,
                    "deprecated_since": dep.deprecated_since.isoformat(),
                    "sunset_at": dep.sunset_at.isoformat() if dep.sunset_at else None,
                    "successor_field": dep.successor_field,
                    "reason": dep.reason,
                }
            )
    return entries


def paths_by_field(body: Any, deprecated_names: set[str]) -> dict[str, list[str]]:
    """把 find_deprecated_paths 的位置列表按字段名归组。"""
    grouped: dict[str, list[str]] = {}
    for location in find_deprecated_paths(body, deprecated_names):
        # 位置形如 $.nickname / $.items[0].nickname，字段名是最后一段点后的键
        leaf = location.rsplit(".", 1)[-1]
        # 去掉可能的数组下标（字段名本身不含 [）
        leaf = leaf.split("[", 1)[0]
        grouped.setdefault(leaf, []).append(location)
    return grouped


def encode_header(entries: list[dict[str, Any]]) -> str:
    """把废弃条目编码进响应头：紧凑、排序键、ASCII 安全（非 ASCII 转义）。"""
    return json.dumps(entries, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def should_annotate(version_deprecations: Iterable[FieldDeprecation]) -> bool:
    """活跃版本没有任何字段废弃登记，永远不产生提醒。"""
    return bool(tuple(version_deprecations))
