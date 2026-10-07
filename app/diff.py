"""历史契约与新生成契约的差异比对与兼容性分类。

纯逻辑模块（不读盘、不依赖 FastAPI）：
- diff_version_contract: 逐条列出变化点，区分 compatible / breaking；
- render_diff_text: 渲染人可读的文本报告；
- ContractDiff.to_dict: 输出程序可解析的结构化报告。

兼容性判定立场（站在"老调用方会不会被弄坏"的角度）：

请求侧（客户端 -> 服务端）：
- 新增字段 / 放宽校验 / 必填变可选 / 新增枚举值：兼容（老请求仍成立）；
- 移除字段：兼容（服务端忽略多传的字段，老调用方不受影响）；
- 收紧校验 / 可选变必填 / 删除枚举值 / 类型收窄：破坏（老请求可能被拒）。

响应侧（服务端 -> 客户端）：
- 新增字段：兼容（老调用方忽略不认识的字段）；
- 删除字段 / 类型改变 / 响应里出现新的枚举取值或可空：破坏（老调用方解析会出问题）。

生命周期：
- 版本进入 sunset（开始拒绝请求）/ sunset 日期提前：破坏；
- active <-> deprecated、废弃日期/后继版本等提示性元数据变化：兼容。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# 变化级别
COMPATIBLE = "compatible"
BREAKING = "breaking"

# 数值/字符串类约束：值变小通常意味着收紧
_LOWER_BOUNDS = {"minLength", "minimum", "minItems", "minProperties"}
_UPPER_BOUNDS = {"maxLength", "maximum", "maxItems", "maxProperties"}
_ALL_BOUNDS = _LOWER_BOUNDS | _UPPER_BOUNDS

_METADATA_KEYS = {"title", "description", "default", "examples"}


@dataclass(frozen=True)
class Change:
    """单个变化点。

    kind: 机器可识别的变化类型；severity: compatible / breaking；
    location: 点号路径，定位到具体端点/字段；message: 人可读说明。
    """

    kind: str
    severity: str
    location: str
    message: str
    old: Any = None
    new: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "old": self.old,
            "new": self.new,
        }


@dataclass
class ContractDiff:
    """单版本契约差异报告。"""

    version: str
    changes: list[Change] = field(default_factory=list)
    snapshot_present: bool = True

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity == BREAKING]

    @property
    def compatible_changes(self) -> list[Change]:
        return [c for c in self.changes if c.severity == COMPATIBLE]

    @property
    def is_compatible(self) -> bool:
        return not self.breaking

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "snapshot_present": self.snapshot_present,
            "compatible": self.is_compatible,
            "breaking_count": len(self.breaking),
            "compatible_change_count": len(self.compatible_changes),
            "changes": [c.to_dict() for c in self.changes],
        }


# --------------------------------------------------------------------------- #
# JSON Schema 归一化辅助
# --------------------------------------------------------------------------- #


def _resolve_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """解析形如 #/$defs/Foo 的本地 $ref。"""
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    node: Any = root
    for part in ref[2:].split("/"):
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _split_members(
    schema: dict[str, Any], root: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """把 anyOf / type 列表展开，返回 (非 null 成员列表, 是否可空)。"""
    schema = _resolve_ref(schema, root)
    nullable = False
    if "anyOf" in schema:
        members: list[dict[str, Any]] = []
        for member in schema["anyOf"]:
            member = _resolve_ref(member, root)
            if member.get("type") == "null":
                nullable = True
            else:
                members.append(member)
        return members, nullable
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        types = list(raw_type)
        if "null" in types:
            nullable = True
            types.remove("null")
        if types:
            normalized = {**schema, "type": types[0] if len(types) == 1 else types}
            return [normalized], nullable
        return [], nullable
    return [schema], False


def _type_signature(schema: dict[str, Any], root: dict[str, Any]) -> tuple[str, ...]:
    """提取可比较的类型签名（忽略 title/description 等元数据）。"""
    schema = _resolve_ref(schema, root)
    if "anyOf" in schema:
        sigs: set[str] = set()
        for member in schema["anyOf"]:
            member = _resolve_ref(member, root)
            sigs.update(_type_signature(member, root))
        sigs.discard("null")
        return tuple(sorted(sigs))
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        return tuple(sorted(t for t in raw_type if t != "null"))
    if raw_type is not None:
        return (raw_type,)
    if "enum" in schema:
        return ("enum",)
    return ("any",)


# --------------------------------------------------------------------------- #
# 单条 schema 差异
# --------------------------------------------------------------------------- #


def _constraint_change(
    key: str, old_v: Any, new_v: Any
) -> str | None:
    """返回 'tighten' / 'loosen' / None（仅对有界约束有意义）。"""
    try:
        old_n = float(old_v)
        new_n = float(new_v)
    except (TypeError, ValueError):
        return None
    if new_n == old_n:
        return None
    if key in _LOWER_BOUNDS:
        return "tighten" if new_n > old_n else "loosen"
    # 上界：变大是放宽
    return "loosen" if new_n > old_n else "tighten"


def _diff_schema(
    old: dict[str, Any],
    new: dict[str, Any],
    root_old: dict[str, Any],
    root_new: dict[str, Any],
    location: str,
    direction: str,
    changes: list[Change],
) -> None:
    """递归比较两个 JSON Schema。direction: request / response。"""
    old = _resolve_ref(old, root_old)
    new = _resolve_ref(new, root_new)
    if not old or not new:
        return

    # --- 类型变化 ---
    old_sig = _type_signature(old, root_old)
    new_sig = _type_signature(new, root_new)
    old_members, old_null = _split_members(old, root_old)
    new_members, new_null = _split_members(new, root_new)

    if old_sig != new_sig:
        if direction == "request":
            # 请求侧：能接受的类型集合变了——保守判定为破坏（老取值可能不再合法）
            changes.append(
                Change(
                    "field_type_changed",
                    BREAKING,
                    location,
                    f"字段类型由 {list(old_sig)} 变为 {list(new_sig)}，老调用方传入的取值可能被拒绝",
                    list(old_sig),
                    list(new_sig),
                )
            )
        else:
            changes.append(
                Change(
                    "field_type_changed",
                    BREAKING,
                    location,
                    f"响应字段类型由 {list(old_sig)} 变为 {list(new_sig)}，老调用方按原类型解析会失败",
                    list(old_sig),
                    list(new_sig),
                )
            )
    else:
        # 仅可空性变化
        if old_null != new_null:
            if direction == "request":
                severity = COMPATIBLE if new_null else BREAKING
                msg = (
                    "字段新增可空：服务端额外接受 null，老调用方不受影响"
                    if new_null
                    else "字段不再允许为 null，仍在传 null 的老调用方会被拒绝"
                )
            else:
                severity = BREAKING if new_null else COMPATIBLE
                msg = (
                    "响应字段新增可空：响应可能出现 null，未做判空的老调用方会出问题"
                    if new_null
                    else "响应字段不再为 null，老调用方不受影响"
                )
            changes.append(Change("field_nullable_changed", severity, location, msg, old_null, new_null))

        # --- 联合类型成员增减（非 null） ---
        old_union = {tuple(s) for m in old_members for s in [_type_signature(m, root_old)]}
        new_union = {tuple(s) for m in new_members for s in [_type_signature(m, root_new)]}
        added = new_union - old_union
        removed = old_union - new_union
        if added or removed:
            if direction == "request":
                if added:
                    changes.append(
                        Change(
                            "field_type_union_added",
                            COMPATIBLE,
                            location,
                            f"字段接受的类型新增 {sorted(added)}，老调用方不受影响",
                        )
                    )
                if removed:
                    changes.append(
                        Change(
                            "field_type_union_removed",
                            BREAKING,
                            location,
                            f"字段不再接受类型 {sorted(removed)}，相关老请求会被拒绝",
                        )
                    )
            else:
                changes.append(
                    Change(
                        "field_type_union_changed",
                        BREAKING,
                        location,
                        f"响应字段可能出现的类型发生变化（新增 {sorted(added)}，移除 {sorted(removed)}）",
                    )
                )

    # --- 枚举值 ---
    old_enum = old.get("enum")
    new_enum = new.get("enum")
    if old_enum is not None or new_enum is not None:
        old_values = set(map(_json_key, old_enum or []))
        new_values = set(map(_json_key, new_enum or []))
        added_v = [json.loads(k) for k in sorted(new_values - old_values)]
        removed_v = [json.loads(k) for k in sorted(old_values - new_values)]
        if added_v:
            severity = COMPATIBLE if direction == "request" else BREAKING
            msg = (
                f"枚举新增取值 {added_v}，老请求不受影响"
                if direction == "request"
                else f"响应枚举可能出现新取值 {added_v}，按枚举穷举处理的老调用方会失配"
            )
            changes.append(Change("field_enum_added", severity, location, msg))
        if removed_v:
            changes.append(
                Change(
                    "field_enum_removed",
                    BREAKING,
                    location,
                    f"枚举取值 {removed_v} 不再可用，仍在使用它的老调用方会出问题",
                )
            )

    # --- 约束 ---
    for key in sorted(_ALL_BOUNDS):
        if key in old and key in new and old[key] != new[key]:
            trend = _constraint_change(key, old[key], new[key])
            if trend is None:
                continue
            if direction == "request" and trend == "tighten":
                changes.append(
                    Change(
                        "field_constraint_tightened",
                        BREAKING,
                        f"{location}.{key}",
                        f"约束 {key} 由 {old[key]} 收紧为 {new[key]}，边界外的老请求会被拒绝",
                        old[key],
                        new[key],
                    )
                )
            else:
                changes.append(
                    Change(
                        "field_constraint_changed",
                        COMPATIBLE,
                        f"{location}.{key}",
                        f"约束 {key} 由 {old[key]} 调整为 {new[key]}（放宽或仅响应侧变化）",
                        old[key],
                        new[key],
                    )
                )
    for key in sorted({"pattern"}):
        if key in old and key in new and old[key] != new[key]:
            severity = BREAKING if direction == "request" else COMPATIBLE
            changes.append(
                Change(
                    "field_pattern_changed",
                    severity,
                    f"{location}.{key}",
                    "请求字段正则约束变化，老取值可能不再合法"
                    if severity == BREAKING
                    else "字段正则约束发生变化（不影响老调用方）",
                    old[key],
                    new[key],
                )
            )

    # --- 废弃标记 ---
    if bool(old.get("deprecated")) != bool(new.get("deprecated")):
        changes.append(
            Change(
                "field_deprecation_changed",
                COMPATIBLE,
                f"{location}.deprecated",
                "字段废弃标记发生变化（仅提示性，不影响线上收发）",
                bool(old.get("deprecated")),
                bool(new.get("deprecated")),
            )
        )

    # --- 提示性元数据 ---
    for key in sorted(_METADATA_KEYS):
        if old.get(key) != new.get(key) and key in old | new:
            changes.append(
                Change(
                    "field_metadata_changed",
                    COMPATIBLE,
                    f"{location}.{key}",
                    f"字段说明性元数据 {key} 发生变化（不影响收发结构）",
                    old.get(key),
                    new.get(key),
                )
            )

    # --- 嵌套对象属性 ---
    old_props = old.get("properties") or {}
    new_props = new.get("properties") or {}
    _diff_properties(old_props, new_props, root_old, root_new, location, direction, changes)

    # --- required 列表 ---
    old_req = set(old.get("required") or [])
    new_req = set(new.get("required") or [])
    for name in sorted(new_req - old_req):
        if direction == "request":
            changes.append(
                Change(
                    "field_became_required",
                    BREAKING,
                    f"{location}.required.{name}",
                    f"字段 {name} 变为必填，未传该字段的老请求会被拒绝",
                )
            )
        else:
            changes.append(
                Change(
                    "response_required_added",
                    COMPATIBLE,
                    f"{location}.required.{name}",
                    f"响应声明新增必填字段 {name}（字段本身的新增已单列）",
                )
            )
    for name in sorted(old_req - new_req):
        changes.append(
            Change(
                "field_became_optional",
                COMPATIBLE,
                f"{location}.required.{name}",
                f"字段 {name} 不再强制要求（老调用方继续传/读都不受影响）",
            )
        )

    # --- 数组元素 ---
    if "items" in old and "items" in new:
        _diff_schema(
            old["items"], new["items"], root_old, root_new, f"{location}[]", direction, changes
        )

    # --- 其余未显式建模的键：结构性变化保守视为破坏 ---
    handled = (
        _ALL_BOUNDS
        | _METADATA_KEYS
        | {
            "anyOf",
            "type",
            "enum",
            "deprecated",
            "properties",
            "required",
            "items",
            "pattern",
            "$ref",
            "$defs",
            "title",
        }
    )
    for key in sorted((set(old) | set(new)) - handled):
        if old.get(key) != new.get(key):
            changes.append(
                Change(
                    "schema_other_changed",
                    BREAKING,
                    f"{location}.{key}",
                    f"结构约束 {key} 发生变化，需人工确认是否影响老调用方",
                    _snippet(old.get(key)),
                    _snippet(new.get(key)),
                )
            )


def _diff_properties(
    old_props: dict[str, Any],
    new_props: dict[str, Any],
    root_old: dict[str, Any],
    root_new: dict[str, Any],
    location: str,
    direction: str,
    changes: list[Change],
) -> None:
    for name in sorted(new_props.keys() - old_props.keys()):
        changes.append(
            Change(
                "field_added",
                COMPATIBLE,
                f"{location}.{name}" if location else name,
                "新增响应字段，老调用方忽略即可"
                if direction == "response"
                else "新增请求字段，老调用方不传不受影响",
            )
        )
    for name in sorted(old_props.keys() - new_props.keys()):
        if direction == "request":
            changes.append(
                Change(
                    "field_removed",
                    COMPATIBLE,
                    f"{location}.{name}" if location else name,
                    f"请求字段 {name} 被移除，继续传该字段的老调用方不受影响（服务端忽略）",
                )
            )
        else:
            changes.append(
                Change(
                    "field_removed",
                    BREAKING,
                    f"{location}.{name}" if location else name,
                    f"响应字段 {name} 被移除，依赖它的老调用方将读不到数据",
                )
            )
    for name in sorted(old_props.keys() & new_props.keys()):
        child_loc = f"{location}.{name}" if location else name
        _diff_schema(
            old_props[name],
            new_props[name],
            root_old,
            root_new,
            child_loc,
            direction,
            changes,
        )


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _snippet(value: Any) -> Any:
    """截断过长的 old/new，避免报告里塞整段 schema。"""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(text) <= 200:
        return value
    return text[:200] + "…"


# --------------------------------------------------------------------------- #
# 端点 / 生命周期 / 错误结构差异
# --------------------------------------------------------------------------- #


def _endpoint_key(ep: dict[str, Any]) -> tuple[str, str]:
    return ep["method"], ep["path"]


def _endpoint_label(method: str, path: str) -> str:
    return f"endpoints[{method} {path}]"


def _diff_endpoints(
    old_eps: list[dict[str, Any]],
    new_eps: list[dict[str, Any]],
    old_root: dict[str, Any],
    new_root: dict[str, Any],
    changes: list[Change],
) -> None:
    old_map = {_endpoint_key(e): e for e in old_eps}
    new_map = {_endpoint_key(e): e for e in new_eps}
    for key in sorted(new_map.keys() - old_map.keys()):
        changes.append(
            Change(
                "endpoint_added",
                COMPATIBLE,
                _endpoint_label(*key),
                "新增端点，不影响已有调用",
            )
        )
    for key in sorted(old_map.keys() - new_map.keys()):
        changes.append(
            Change(
                "endpoint_removed",
                BREAKING,
                _endpoint_label(*key),
                "端点被移除，老调用方对它的请求将失败",
            )
        )
    for key in sorted(old_map.keys() & new_map.keys()):
        old_ep = old_map[key]
        new_ep = new_map[key]
        label = _endpoint_label(*key)
        if old_ep.get("summary") != new_ep.get("summary"):
            changes.append(
                Change(
                    "endpoint_summary_changed",
                    COMPATIBLE,
                    f"{label}.summary",
                    "端点说明发生变化（不影响收发结构）",
                    old_ep.get("summary"),
                    new_ep.get("summary"),
                )
            )
        old_req, new_req = old_ep.get("request"), new_ep.get("request")
        req_loc = f"{label}.request"
        if old_req is None and new_req is not None:
            changes.append(
                Change(
                    "request_schema_added",
                    BREAKING,
                    req_loc,
                    "端点新增了请求体要求，原来不带请求体的老调用方可能被拒绝",
                )
            )
        elif old_req is not None and new_req is None:
            changes.append(
                Change(
                    "request_schema_removed",
                    COMPATIBLE,
                    req_loc,
                    "端点不再要求请求体，老调用方继续发送不受影响",
                )
            )
        elif old_req is not None and new_req is not None:
            _diff_schema(old_req, new_req, old_root, new_root, req_loc, "request", changes)

        old_resp, new_resp = old_ep.get("response"), new_ep.get("response")
        resp_loc = f"{label}.response"
        if old_resp is None and new_resp is not None:
            changes.append(
                Change(
                    "response_schema_added",
                    COMPATIBLE,
                    resp_loc,
                    "端点补充了响应结构声明，不破坏已有调用",
                )
            )
        elif old_resp is not None and new_resp is None:
            changes.append(
                Change(
                    "response_schema_removed",
                    BREAKING,
                    resp_loc,
                    "响应结构声明被移除，老调用方依赖的返回字段不再有保障",
                )
            )
        elif old_resp is not None and new_resp is not None:
            _diff_schema(old_resp, new_resp, old_root, new_root, resp_loc, "response", changes)


# 版本元数据里纯提示性的键（变化永远兼容）
_VERSION_META_KEYS = ("name", "deprecated_since", "successor", "deprecated_fields",
                      "deprecated_field_details")


def _diff_version_meta(
    old_meta: dict[str, Any], new_meta: dict[str, Any], changes: list[Change]
) -> None:
    old_status = old_meta.get("status")
    new_status = new_meta.get("status")
    if old_status != new_status:
        if new_status == "sunset":
            changes.append(
                Change(
                    "version_status_changed",
                    BREAKING,
                    "version.status",
                    f"版本状态由 {old_status} 变为 sunset，该版本请求将被一律拒绝（410）",
                    old_status,
                    new_status,
                )
            )
        else:
            changes.append(
                Change(
                    "version_status_changed",
                    COMPATIBLE,
                    "version.status",
                    f"版本状态由 {old_status} 变为 {new_status}，服务仍正常响应",
                    old_status,
                    new_status,
                )
            )

    old_sunset = old_meta.get("sunset_at")
    new_sunset = new_meta.get("sunset_at")
    if old_sunset != new_sunset:
        if new_sunset is None:
            changes.append(
                Change(
                    "version_sunset_cleared",
                    COMPATIBLE,
                    "version.sunset_at",
                    "下线日期被取消，老调用方获得更长迁移窗口",
                    old_sunset,
                    None,
                )
            )
        elif old_sunset is None:
            changes.append(
                Change(
                    "version_sunset_announced",
                    COMPATIBLE,
                    "version.sunset_at",
                    f"新公布计划下线日期 {new_sunset}，当前仍正常服务",
                    None,
                    new_sunset,
                )
            )
        elif new_sunset < old_sunset:
            changes.append(
                Change(
                    "version_sunset_moved_earlier",
                    BREAKING,
                    "version.sunset_at",
                    f"下线日期由 {old_sunset} 提前到 {new_sunset}，迁移窗口缩短",
                    old_sunset,
                    new_sunset,
                )
            )
        else:
            changes.append(
                Change(
                    "version_sunset_moved_later",
                    COMPATIBLE,
                    "version.sunset_at",
                    f"下线日期由 {old_sunset} 延后到 {new_sunset}，老调用方不受损",
                    old_sunset,
                    new_sunset,
                )
            )

    for key in _VERSION_META_KEYS:
        if old_meta.get(key) != new_meta.get(key) and key in old_meta | new_meta:
            changes.append(
                Change(
                    "version_metadata_changed",
                    COMPATIBLE,
                    f"version.{key}",
                    f"版本提示性元数据 {key} 发生变化（不影响线上收发）",
                    _snippet(old_meta.get(key)),
                    _snippet(new_meta.get(key)),
                )
            )


def _diff_errors(
    old_errors: dict[str, Any],
    new_errors: dict[str, Any],
    old_root: dict[str, Any],
    new_root: dict[str, Any],
    changes: list[Change],
) -> None:
    old_codes = set(old_errors.get("codes") or [])
    new_codes = set(new_errors.get("codes") or [])
    for code in sorted(new_codes - old_codes):
        changes.append(
            Change(
                "error_code_added",
                COMPATIBLE,
                f"errors.codes.{code}",
                f"新增错误码 {code}，老调用方按既有错误处理逻辑兜底即可",
            )
        )
    for code in sorted(old_codes - new_codes):
        changes.append(
            Change(
                "error_code_removed",
                COMPATIBLE,
                f"errors.codes.{code}",
                f"错误码 {code} 不再出现（对应失败场景已移除）",
            )
        )
    old_env = old_errors.get("envelope") or {}
    new_env = new_errors.get("envelope") or {}
    if old_env and new_env:
        _diff_schema(old_env, new_env, old_root, new_root, "errors.envelope", "response", changes)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def diff_version_contract(
    old: dict[str, Any] | None, new: dict[str, Any], version: str
) -> ContractDiff:
    """比对同一版本的历史契约与新生成契约。

    old 为 None 表示历史快照不存在（首次落盘），全部按"新增/兼容"处理。
    """
    report = ContractDiff(version=version, changes=[], snapshot_present=old is not None)
    changes = report.changes
    if old is None:
        changes.append(
            Change(
                "version_snapshot_created",
                COMPATIBLE,
                "version",
                f"版本 {version} 首次生成契约快照，不存在历史约束",
            )
        )
        for ep in sorted(new.get("endpoints") or [], key=_endpoint_key):
            changes.append(
                Change(
                    "endpoint_added",
                    COMPATIBLE,
                    _endpoint_label(ep["method"], ep["path"]),
                    "新增端点，不影响已有调用",
                )
            )
        return report

    _diff_version_meta(old.get("version") or {}, new.get("version") or {}, changes)
    _diff_endpoints(
        old.get("endpoints") or [],
        new.get("endpoints") or [],
        old,
        new,
        changes,
    )
    _diff_errors(old.get("errors") or {}, new.get("errors") or {}, old, new, changes)

    # 顶层未建模键
    handled = {"version", "endpoints", "errors"}
    for key in sorted((set(old) | set(new)) - handled):
        if old.get(key) != new.get(key):
            changes.append(
                Change(
                    "contract_section_changed",
                    COMPATIBLE,
                    key,
                    f"契约附加段 {key} 发生变化（不影响既有收发结构）",
                    _snippet(old.get(key)),
                    _snippet(new.get(key)),
                )
            )
    return report


def render_diff_text(report: ContractDiff) -> str:
    """把差异报告渲染为人可读文本（确定性输出）。"""
    lines = [f"契约差异报告: {report.version}"]
    if not report.snapshot_present:
        lines.append("  历史快照不存在：首次生成，无破坏性变更。")
        lines.append("")
        for c in report.changes:
            lines.append(f"  [兼容] {c.location} - {c.message}")
        return "\n".join(lines) + "\n"
    if not report.changes:
        lines.append("  结论: 无变化（逐字节一致）。")
        return "\n".join(lines) + "\n"
    lines.append(
        f"  结论: {len(report.breaking)} 处破坏性变更，"
        f"{len(report.compatible_changes)} 处兼容变更"
    )
    lines.append(
        "  判定: "
        + ("存在破坏性变更，默认拒绝写入历史契约。" if report.breaking else "全部为兼容变更，允许写入。")
    )
    for c in report.changes:
        tag = "破坏" if c.severity == BREAKING else "兼容"
        lines.append(f"  [{tag}] {c.location} - {c.message}")
        if c.old is not None or c.new is not None:
            lines.append(f"      旧: {json.dumps(c.old, ensure_ascii=False, sort_keys=True)}")
            lines.append(f"      新: {json.dumps(c.new, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"
