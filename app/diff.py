"""契约差异与兼容性分析。

把新生成的契约与历史契约逐版本对比，输出人可读、也可被程序解析的差异报告，
并把每条变化分类为：

- ``compatible``：对老调用方没有影响的兼容变化（新增端点、新增可选请求字段、
  响应新增字段、约束放宽、标记废弃等）；
- ``breaking``：会让仍在使用老契约的调用方出问题的破坏性变更
  （移除端点/字段、新增必填请求字段、收紧校验、响应字段可能变为空、
  版本进入下线等），报告会说明破坏在哪里。

判定原则（面向“老调用方会不会坏”）：

- 请求方向：服务端必须继续接受老调用方会发送的一切。类型收窄、字段变必填、
  校验收紧、字段被移除都属于破坏性；反之放宽属于兼容。
- 响应方向：老调用方按旧契约解析响应。移除字段、字段可能出现新类型
  （如非空变可空）属于破坏性；新增字段属于兼容。
"""
from __future__ import annotations

from typing import Any

BREAKING = "breaking"
COMPATIBLE = "compatible"

# 参与兼容性判定的校验约束；其它 JSON Schema 关键字（title/description 等）忽略。
_INCREASE_TIGHTENS = ("minLength", "minimum", "minItems", "minProperties")
_DECREASE_TIGHTENS = ("maxLength", "maximum", "maxItems", "maxProperties")
_CHANGE_TIGHTENS = ("pattern",)
_CONSTRAINT_KEYS = _INCREASE_TIGHTENS + _DECREASE_TIGHTENS + _CHANGE_TIGHTENS


# ---------------------------------------------------------------------------
# JSON Schema 归一化
# ---------------------------------------------------------------------------

def _deref(node: Any, defs: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:  # 防御性：遇到循环引用就停在当前节点
            break
        seen.add(ref)
        node = defs.get(ref.rsplit("/", 1)[-1], {})
    return node if isinstance(node, dict) else {}


def _collect_fields(
    node: Any,
    defs: dict[str, Any],
    prefix: str,
    required: bool,
    out: dict[str, dict[str, Any]],
) -> None:
    """把（可能含 $ref/anyOf/嵌套对象的）schema 展平成 path -> 字段描述。"""
    node = _deref(node, defs)
    branches: list[dict[str, Any]] = [node]
    for combiner in ("anyOf", "oneOf", "allOf"):
        if combiner in node:
            branches.extend(b for b in node[combiner])

    types: set[str] = set()
    constraints: dict[str, Any] = {}
    deprecated = False
    object_like = False
    for branch in branches:
        branch = _deref(branch, defs)
        for combiner in ("anyOf", "oneOf", "allOf"):
            if combiner in branch:
                _collect_fields(branch[combiner], defs, prefix, required, out)
        t = branch.get("type")
        if isinstance(t, list):
            types.update(t)
        elif t:
            types.add(t)
        for key in _CONSTRAINT_KEYS:
            if key in branch:
                constraints[key] = branch[key]
        deprecated = deprecated or bool(branch.get("deprecated"))
        if "properties" in branch:
            object_like = True
            req = set(branch.get("required", []))
            for name, prop in branch["properties"].items():
                child_prefix = f"{prefix}.{name}" if prefix else name
                child_required = required and name in req
                _collect_fields(prop, defs, child_prefix, child_required, out)
                if child_prefix not in out:
                    out[child_prefix] = {}
                out[child_prefix].setdefault("types", set())
                # 直接子字段的 required 由本层决定
                out[child_prefix]["required"] = child_required

    if prefix:
        entry = out.setdefault(prefix, {})
        entry.setdefault("types", set()).update(types)
        entry.setdefault("constraints", {}).update(constraints)
        if deprecated:
            entry["deprecated"] = True
        entry.setdefault("deprecated", False)
        entry["required"] = required
        # 对象自身没有标量类型时，用 object 占位
        if not entry["types"] and object_like:
            entry["types"].add("object")


def _normalize_schema(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
    """把端点模型 schema 归一化为 {字段路径: {types,required,constraints,deprecated}}。"""
    if not schema:
        return None
    defs = schema.get("$defs", {})
    fields: dict[str, dict[str, Any]] = {}
    _collect_fields(schema, defs, "", True, fields)
    fields.pop("", None)
    return fields


def _freeze(value: Any) -> Any:
    """把字段描述转成可比较、可序列化的确定性形式。"""
    return {
        "types": sorted(value.get("types", ())),
        "required": bool(value.get("required")),
        "constraints": dict(sorted(value.get("constraints", {}).items())),
        "deprecated": bool(value.get("deprecated")),
    }


def _normalize_endpoint_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    fields = _normalize_schema(schema) or {}
    return {path: _freeze(desc) for path, desc in sorted(fields.items())}


# ---------------------------------------------------------------------------
# 变化点收集
# ---------------------------------------------------------------------------

class _Changes:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        category: str,
        kind: str,
        path: str,
        message: str,
        before: Any = None,
        after: Any = None,
    ) -> None:
        self.items.append(
            {
                "kind": kind,
                "category": category,
                "path": path,
                "message": message,
                "before": before,
                "after": after,
            }
        )

    def finalize(self) -> list[dict[str, Any]]:
        return sorted(self.items, key=lambda c: (c["path"], c["category"], c["kind"]))


def _constraint_change(key: str, old: Any, new: Any, present: tuple) -> str | None:
    """返回 'tightened' / 'loosened' / None；present 标记约束是否新增/移除。"""
    old_present, new_present = present
    if not old_present and not new_present:
        return None
    if old_present and not new_present:
        return "loosened"
    if not old_present and new_present:
        return "tightened"
    if old == new:
        return None
    if key in _CHANGE_TIGHTENS:
        return "tightened"
    try:
        if key in _INCREASE_TIGHTENS:
            return "tightened" if new > old else "loosened"
        if key in _DECREASE_TIGHTENS:
            return "tightened" if new < old else "loosened"
    except TypeError:
        return "tightened"
    return None


def _compare_model_fields(
    changes: _Changes,
    direction: str,
    base_path: str,
    old_fields: dict[str, Any],
    new_fields: dict[str, Any],
) -> None:
    """对比请求或响应模型的归一化字段集。"""
    old_names = set(old_fields)
    new_names = set(new_fields)

    for name in sorted(old_names - new_names):
        if direction == "request":
            changes.add(
                BREAKING,
                "request_field_removed",
                f"{base_path}.{name}",
                f"请求字段 {name} 被移除：仍发送该字段的老调用方将不再被契约接受",
                old_fields[name],
                None,
            )
        else:
            changes.add(
                BREAKING,
                "response_field_removed",
                f"{base_path}.{name}",
                f"响应不再返回字段 {name}：读取该字段的老调用方会拿到空值/缺字段",
                old_fields[name],
                None,
            )

    for name in sorted(new_names - old_names):
        if direction == "request":
            if new_fields[name]["required"]:
                changes.add(
                    BREAKING,
                    "request_field_added_required",
                    f"{base_path}.{name}",
                    f"请求新增必填字段 {name}：不传该字段的老调用方会被拒绝",
                    None,
                    new_fields[name],
                )
            else:
                changes.add(
                    COMPATIBLE,
                    "request_field_added_optional",
                    f"{base_path}.{name}",
                    f"请求新增可选字段 {name}：老调用方无需改动",
                    None,
                    new_fields[name],
                )
        else:
            changes.add(
                COMPATIBLE,
                "response_field_added",
                f"{base_path}.{name}",
                f"响应新增字段 {name}：老调用方忽略多余字段即可",
                None,
                new_fields[name],
            )

    for name in sorted(old_names & new_names):
        old_f = old_fields[name]
        new_f = new_fields[name]
        path = f"{base_path}.{name}"

        # 必填性
        if old_f["required"] != new_f["required"]:
            if direction == "request" and new_f["required"]:
                changes.add(
                    BREAKING,
                    "request_field_now_required",
                    path,
                    f"请求字段 {name} 由可选变为必填：老调用方的请求会被拒绝",
                    {"required": False},
                    {"required": True},
                )
            elif direction == "response" and not new_f["required"]:
                changes.add(
                    BREAKING,
                    "response_field_now_optional",
                    path,
                    f"响应字段 {name} 由必有变为可缺省：依赖其存在的老调用方会出问题",
                    {"required": True},
                    {"required": False},
                )
            else:
                changes.add(
                    COMPATIBLE,
                    "field_required_loosened",
                    path,
                    f"字段 {name} 的必填要求放宽，对老调用方无影响",
                    {"required": old_f["required"]},
                    {"required": new_f["required"]},
                )

        # 类型
        old_types = set(old_f["types"])
        new_types = set(new_f["types"])
        if old_types != new_types:
            if direction == "request":
                broken = not old_types <= new_types
                reason = "服务端不再接受老调用方可能发送的类型"
            else:
                broken = not new_types <= old_types
                reason = "响应可能出现老调用方按旧类型无法解析的值"
            if broken:
                changes.add(
                    BREAKING,
                    f"{direction}_field_type_changed",
                    path,
                    f"{('请求' if direction == 'request' else '响应')}字段 {name} 类型变化：{reason}"
                    f"（{sorted(old_types)} -> {sorted(new_types)}）",
                    {"types": sorted(old_types)},
                    {"types": sorted(new_types)},
                )
            else:
                changes.add(
                    COMPATIBLE,
                    f"{direction}_field_type_widened",
                    path,
                    f"{('请求' if direction == 'request' else '响应')}字段 {name} 类型放宽"
                    f"（{sorted(old_types)} -> {sorted(new_types)}），老调用方不受影响",
                    {"types": sorted(old_types)},
                    {"types": sorted(new_types)},
                )

        # 约束
        for key in _CONSTRAINT_KEYS:
            old_has = key in old_f["constraints"]
            new_has = key in new_f["constraints"]
            effect = _constraint_change(
                key,
                old_f["constraints"].get(key),
                new_f["constraints"].get(key),
                (old_has, new_has),
            )
            if effect is None:
                continue
            label = "收紧" if effect == "tightened" else "放宽"
            if effect == "tightened":
                if direction == "request":
                    message = (
                        f"请求字段 {name} 的约束 {key} {label}：原本合法的老调用方请求可能被拒绝"
                    )
                else:
                    message = (
                        f"响应字段 {name} 的约束 {key} {label}：老调用方按原取值范围的假设可能失效"
                    )
                changes.add(
                    BREAKING,
                    f"{direction}_constraint_tightened",
                    path,
                    message,
                    {"constraints": {k: v for k, v in old_f["constraints"].items() if k == key}},
                    {"constraints": {k: v for k, v in new_f["constraints"].items() if k == key}},
                )
            else:
                changes.add(
                    COMPATIBLE,
                    f"{direction}_constraint_loosened",
                    path,
                    f"{'请求' if direction == 'request' else '响应'}字段 {name} 的约束 {key} {label}",
                    old_f["constraints"].get(key),
                    new_f["constraints"].get(key),
                )

        # 废弃标记
        if old_f["deprecated"] != new_f["deprecated"]:
            if new_f["deprecated"]:
                changes.add(
                    COMPATIBLE,
                    "field_deprecated",
                    path,
                    f"字段 {name} 被标记为废弃（仍可用，仅提醒调用方迁移）",
                    {"deprecated": False},
                    {"deprecated": True},
                )
            else:
                changes.add(
                    COMPATIBLE,
                    "field_undeprecated",
                    path,
                    f"字段 {name} 取消废弃标记",
                    {"deprecated": True},
                    {"deprecated": False},
                )


def _endpoint_key(endpoint: dict[str, Any]) -> str:
    return f"{endpoint['method']} {endpoint['path']}"


def _compare_endpoints(changes: _Changes, old: list, new: list) -> None:
    old_map = {_endpoint_key(e): e for e in old}
    new_map = {_endpoint_key(e): e for e in new}

    for key in sorted(set(old_map) - set(new_map)):
        changes.add(
            BREAKING,
            "endpoint_removed",
            key,
            f"端点 {key} 被移除：老调用方请求将得到 404",
            True,
            None,
        )
    for key in sorted(set(new_map) - set(old_map)):
        changes.add(
            COMPATIBLE,
            "endpoint_added",
            key,
            f"新增端点 {key}：不影响老调用方",
            None,
            True,
        )

    for key in sorted(set(old_map) & set(new_map)):
        old_req = _normalize_endpoint_schema(old_map[key].get("request"))
        new_req = _normalize_endpoint_schema(new_map[key].get("request"))
        _compare_model_fields(changes, "request", f"{key} request", old_req, new_req)
        old_resp = _normalize_endpoint_schema(old_map[key].get("response"))
        new_resp = _normalize_endpoint_schema(new_map[key].get("response"))
        _compare_model_fields(changes, "response", f"{key} response", old_resp, new_resp)


def _compare_version_block(changes: _Changes, old: dict | None, new: dict | None) -> None:
    path = "version"
    if old is None and new is not None:
        changes.add(COMPATIBLE, "version_added", path, f"新版本 {new['name']} 登记上线", None, new)
        return
    if old is not None and new is None:
        changes.add(BREAKING, "version_removed", path, f"版本 {old['name']} 被移除", old, None)
        return
    assert old is not None and new is not None

    if old["status"] != new["status"]:
        if new["status"] == "sunset":
            changes.add(
                BREAKING,
                "version_status_changed",
                path,
                f"版本 {new['name']} 状态 {old['status']} -> {new['status']}："
                "进入下线后所有老调用方请求都会被拒绝",
                {"status": old["status"]},
                {"status": new["status"]},
            )
        else:
            changes.add(
                COMPATIBLE,
                "version_status_changed",
                path,
                f"版本 {new['name']} 状态 {old['status']} -> {new['status']}，老调用方可继续使用",
                {"status": old["status"]},
                {"status": new["status"]},
            )

    old_sunset = old.get("sunset_at")
    new_sunset = new.get("sunset_at")
    if old_sunset != new_sunset:
        if old_sunset is None and new_sunset is not None:
            changes.add(
                BREAKING,
                "sunset_date_added",
                path,
                f"版本 {new['name']} 新增下线日期 {new_sunset}：到达当日起老调用方会被拒绝",
                {"sunset_at": None},
                {"sunset_at": new_sunset},
            )
        elif old_sunset is not None and new_sunset is None:
            changes.add(
                COMPATIBLE,
                "sunset_date_cleared",
                path,
                f"版本 {new['name']} 取消下线日期（原 {old_sunset}）",
                {"sunset_at": old_sunset},
                {"sunset_at": None},
            )
        elif new_sunset < old_sunset:
            changes.add(
                BREAKING,
                "sunset_date_moved_earlier",
                path,
                f"版本 {new['name']} 下线日期提前：{old_sunset} -> {new_sunset}，"
                "老调用方的迁移窗口缩短",
                {"sunset_at": old_sunset},
                {"sunset_at": new_sunset},
            )
        else:
            changes.add(
                COMPATIBLE,
                "sunset_date_moved_later",
                path,
                f"版本 {new['name']} 下线日期推迟：{old_sunset} -> {new_sunset}",
                {"sunset_at": old_sunset},
                {"sunset_at": new_sunset},
            )

    for key, label in (("deprecated_since", "废弃起始日期"), ("successor", "后继版本")):
        if old.get(key) != new.get(key):
            changes.add(
                COMPATIBLE,
                f"version_{key}_changed",
                path,
                f"版本 {new['name']} {label}变化：{old.get(key)} -> {new.get(key)}（仅提醒）",
                {key: old.get(key)},
                {key: new.get(key)},
            )

    old_dep_fields = set(old.get("deprecated_fields", {}))
    new_dep_fields = set(new.get("deprecated_fields", {}))
    for name in sorted(new_dep_fields - old_dep_fields):
        changes.add(
            COMPATIBLE,
            "field_deprecated",
            f"version.deprecated_fields.{name}",
            f"字段 {name} 被登记为废弃（{new['deprecated_fields'][name]} 起），仍可继续使用",
            None,
            {"field": name, "deprecated_since": new["deprecated_fields"][name]},
        )
    for name in sorted(old_dep_fields - new_dep_fields):
        changes.add(
            COMPATIBLE,
            "field_undeprecated",
            f"version.deprecated_fields.{name}",
            f"字段 {name} 取消废弃登记",
            {"field": name, "deprecated_since": old["deprecated_fields"][name]},
            None,
        )


def _compare_errors(changes: _Changes, old: dict, new: dict) -> None:
    old_codes = set(old.get("codes", []))
    new_codes = set(new.get("codes", []))
    for code in sorted(old_codes - new_codes):
        changes.add(
            BREAKING,
            "error_code_removed",
            f"errors.codes.{code}",
            f"错误码 {code} 被移除：按该码做分支处理的老调用方会失配",
            code,
            None,
        )
    for code in sorted(new_codes - old_codes):
        changes.add(
            COMPATIBLE,
            "error_code_added",
            f"errors.codes.{code}",
            f"新增错误码 {code}：老调用方的既有分支不受影响",
            None,
            code,
        )
    old_env = _normalize_endpoint_schema(old.get("envelope"))
    new_env = _normalize_endpoint_schema(new.get("envelope"))
    _compare_model_fields(changes, "response", "errors.envelope", old_env, new_env)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _report(version: str | None, changes: _Changes) -> dict[str, Any]:
    items = changes.finalize()
    breaking = [c for c in items if c["category"] == BREAKING]
    compatible = [c for c in items if c["category"] == COMPATIBLE]
    return {
        "version": version,
        "has_changes": bool(items),
        "has_breaking_changes": bool(breaking),
        "breaking_change_count": len(breaking),
        "compatible_change_count": len(compatible),
        "changes": items,
    }


def diff_version_contracts(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    *,
    version: str,
) -> dict[str, Any]:
    """对比单个版本的契约切片（``contract_for_version`` 的产物）。

    old 为 None 表示历史上不存在该版本契约；new 为 None 表示版本被移除。
    """
    changes = _Changes()
    old_block = old["version"] if old else None
    new_block = new["version"] if new else None
    _compare_version_block(changes, old_block, new_block)
    _compare_endpoints(
        changes,
        old.get("endpoints", []) if old else [],
        new.get("endpoints", []) if new else [],
    )
    if old and new:
        _compare_errors(changes, old.get("errors", {}), new.get("errors", {}))
    return _report(version, changes)


def diff_contracts(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    """对比全量契约（``generate_contract`` 的产物），聚合各版本差异报告。"""
    from app.docs import contract_for_version

    old = old or {}
    new = new or {}
    old_names = [v["name"] for v in old.get("versions", [])]
    new_names = [v["name"] for v in new.get("versions", [])]
    reports: dict[str, dict[str, Any]] = {}
    for name in sorted(set(old_names) | set(new_names)):
        old_slice = contract_for_version(old, name) if name in old_names else None
        new_slice = contract_for_version(new, name) if name in new_names else None
        reports[name] = diff_version_contracts(old_slice, new_slice, version=name)

    all_changes = [c for r in reports.values() for c in r["changes"]]
    return {
        "versions": reports,
        "has_changes": bool(all_changes),
        "has_breaking_changes": any(r["has_breaking_changes"] for r in reports.values()),
        "breaking_change_count": sum(r["breaking_change_count"] for r in reports.values()),
        "compatible_change_count": sum(r["compatible_change_count"] for r in reports.values()),
    }
