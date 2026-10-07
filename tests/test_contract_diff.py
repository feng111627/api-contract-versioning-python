"""场景：契约差异比对与破坏性变更门禁。

- 兼容变化：请求新增可选字段、响应新增字段、必填变可选、约束放宽、
  新增枚举值、废弃标记、版本提示性元数据；
- 破坏性变化：响应删字段、请求新增必填、请求约束收紧、删枚举值、
  端点删除、版本提前下线；
- 破坏性变更默认不允许写进历史快照，allow_update 只放行兼容变更，
  必须再显式 allow_breaking 才能落盘破坏性变更；
- 差异报告可通过 /contract/diff 与 /contract/diff/{version} 拿到（JSON 与文本）。
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from app.diff import (
    ContractDiff,
    diff_version_contract,
    render_diff_text,
)
from app.docs import (
    BreakingContractChangeError,
    SnapshotConflictError,
    contract_for_version,
    diff_snapshot,
    generate_contract,
    render_json,
    write_snapshots,
)

logger = logging.getLogger("tests.scenarios")


def _base_contract() -> dict:
    return {
        "version": {"name": "v1", "status": "deprecated", "sunset_at": "2026-12-31"},
        "endpoints": [
            {
                "version": "v1",
                "method": "POST",
                "path": "/v1/users",
                "summary": "create_user",
                "request": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 50},
                        "kind": {"type": "string", "enum": ["a", "b"]},
                    },
                    "required": ["name"],
                },
                "response": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                        "kind": {"type": "string", "enum": ["a", "b"]},
                    },
                    "required": ["id", "name"],
                },
            }
        ],
        "errors": {"codes": ["NOT_FOUND", "VALIDATION_FAILED"], "envelope": {"type": "object"}},
    }


def _kinds(report: ContractDiff, severity: str | None = None) -> set[str]:
    return {c.kind for c in report.changes if severity is None or c.severity == severity}


# ----------------------------- 兼容性分类 ---------------------------------- #


def test_no_change_means_empty_report():
    logger.info("用例: 新旧契约完全一致时没有任何变化点")
    base = _base_contract()
    report = diff_version_contract(base, copy.deepcopy(base), "v1")
    assert report.is_compatible
    assert report.changes == []
    assert len(report.breaking) == 0


def test_optional_request_field_added_is_compatible():
    logger.info("用例: 请求新增可选字段判为兼容")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["nickname"] = {"type": ["string", "null"]}
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_added" in _kinds(report)


def test_response_field_added_is_compatible():
    logger.info("用例: 响应新增字段判为兼容（老调用方忽略）")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["response"]["properties"]["age"] = {"type": "integer"}
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_added" in _kinds(report)


def test_request_field_became_optional_is_compatible():
    logger.info("用例: 请求必填字段变可选判为兼容")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["required"] = []
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_became_optional" in _kinds(report)


def test_request_constraint_relaxed_is_compatible():
    logger.info("用例: 请求约束放宽（minLength 变小）判为兼容")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["name"]["minLength"] = 1
    new["endpoints"][0]["request"]["properties"]["name"]["maxLength"] = 100
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_constraint_changed" in _kinds(report)


def test_request_enum_value_added_is_compatible():
    logger.info("用例: 请求枚举新增取值判为兼容")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["kind"]["enum"] = ["a", "b", "c"]
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_enum_added" in _kinds(report)


def test_request_field_removed_is_compatible():
    logger.info("用例: 服务端不再接受某请求字段对老调用方兼容（多传被忽略）")
    old = _base_contract()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["request"]["properties"]["kind"]
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible


def test_deprecation_and_lifecycle_metadata_are_compatible():
    logger.info("用例: 字段废弃标记与版本提示性元数据变化判为兼容")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["response"]["properties"]["name"] = {
        "type": "string",
        "deprecated": True,
        "description": "废弃说明",
    }
    new["version"]["successor"] = "v2"
    new["version"]["deprecated_since"] = "2026-01-15"
    report = diff_version_contract(old, new, "v1")
    assert report.is_compatible
    assert "field_deprecation_changed" in _kinds(report)
    assert "version_metadata_changed" in _kinds(report)


# ----------------------------- 破坏性变更 ---------------------------------- #


def test_response_field_removed_is_breaking():
    logger.info("用例: 响应删除老调用方依赖的字段判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["response"]["properties"]["name"]
    report = diff_version_contract(old, new, "v1")
    assert not report.is_compatible
    breaking = report.breaking
    assert any(c.kind == "field_removed" and "name" in c.message for c in breaking)


def test_required_request_field_added_is_breaking():
    logger.info("用例: 请求新增必填字段判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["email"] = {"type": "string"}
    new["endpoints"][0]["request"]["required"] = ["name", "email"]
    report = diff_version_contract(old, new, "v1")
    assert not report.is_compatible
    assert any(c.kind == "field_became_required" for c in report.breaking)


def test_request_constraint_tightened_is_breaking():
    logger.info("用例: 请求约束收紧（minLength 变大）判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["name"]["minLength"] = 10
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "field_constraint_tightened" for c in report.breaking)


def test_request_enum_value_removed_is_breaking():
    logger.info("用例: 请求枚举删除取值判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["kind"]["enum"] = ["a"]
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "field_enum_removed" for c in report.breaking)


def test_response_enum_value_added_is_breaking():
    logger.info("用例: 响应枚举出现新取值对按枚举穷举的老调用方是破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["response"]["properties"]["kind"]["enum"] = ["a", "b", "c"]
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "field_enum_added" for c in report.breaking)


def test_response_field_nullable_added_is_breaking():
    logger.info("用例: 响应字段新增可空判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["response"]["properties"]["name"] = {"type": ["string", "null"]}
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "field_nullable_changed" for c in report.breaking)


def test_endpoint_removed_is_breaking():
    logger.info("用例: 端点被删除判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"] = []
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "endpoint_removed" for c in report.breaking)


def test_version_sunset_moved_earlier_is_breaking():
    logger.info("用例: 版本下线日期提前判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["version"]["sunset_at"] = "2026-06-30"
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "version_sunset_moved_earlier" for c in report.breaking)


def test_version_status_to_sunset_is_breaking():
    logger.info("用例: 版本直接变为 sunset（请求将被拒绝）判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["version"]["status"] = "sunset"
    report = diff_version_contract(old, new, "v1")
    assert any(c.kind == "version_status_changed" for c in report.breaking)


def test_first_snapshot_is_never_breaking():
    logger.info("用例: 历史快照不存在（首次落盘）一律视为兼容")
    report = diff_version_contract(None, _base_contract(), "v9")
    assert report.is_compatible
    assert report.snapshot_present is False
    assert any(c.kind == "version_snapshot_created" for c in report.changes)


def test_mixed_compatible_and_breaking_changes_are_separated():
    logger.info("用例: 同一份演进同时含兼容与破坏变化时，逐条正确归类并整体判为破坏")
    old = _base_contract()
    new = copy.deepcopy(old)
    # 兼容：响应新增字段
    new["endpoints"][0]["response"]["properties"]["age"] = {"type": "integer"}
    # 兼容：请求字段标废弃
    new["endpoints"][0]["request"]["properties"]["name"] = {
        "type": "string", "minLength": 1, "maxLength": 50, "deprecated": True
    }
    # 破坏：响应删除字段
    del new["endpoints"][0]["response"]["properties"]["kind"]
    report = diff_version_contract(old, new, "v1")
    assert not report.is_compatible
    assert any(c.kind == "field_added" for c in report.compatible_changes)
    assert any(c.kind == "field_deprecation_changed" for c in report.compatible_changes)
    assert any(c.kind == "field_removed" for c in report.breaking)
    assert report.to_dict()["breaking_count"] >= 1
    assert report.to_dict()["compatible_change_count"] >= 2


# ----------------------------- 报告形态 ------------------------------------ #


def test_report_is_machine_and_human_readable():
    logger.info("用例: 报告既有程序可解析结构，也能渲染人可读文本")
    old = _base_contract()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["response"]["properties"]["name"]
    new["endpoints"][0]["response"]["required"].remove("name")
    report = diff_version_contract(old, new, "v1")

    data = report.to_dict()
    assert data["version"] == "v1"
    assert data["compatible"] is False
    assert data["breaking_count"] >= 1
    assert all({"kind", "severity", "location", "message", "old", "new"} <= set(c)
               for c in data["changes"])

    text = render_diff_text(report)
    assert "破坏性变更" in text
    assert "[破坏]" in text
    assert "默认拒绝写入" in text


def test_report_rendering_is_deterministic():
    logger.info("用例: 同一差异多次渲染逐字节一致")
    old = _base_contract()
    new = copy.deepcopy(old)
    new["endpoints"][0]["response"]["properties"]["age"] = {"type": "integer"}
    report = diff_version_contract(old, new, "v1")
    first = render_diff_text(report)
    assert render_diff_text(diff_version_contract(old, new, "v1")) == first


# --------------------------- 写盘门禁集成 ---------------------------------- #


def test_compatible_change_needs_allow_update(app_and_registry, tmp_path):
    logger.info("用例: 兼容变化默认也不覆盖历史，allow_update 后可写入")
    app, registry = app_and_registry
    contract = generate_contract(app, registry)
    v1 = json.loads(render_json(contract_for_version(contract, "v1")))
    v1_original = json.loads(render_json(contract_for_version(contract, "v1")))

    # 历史快照里有一个新代码不再产出的响应字段 -> 破坏
    for ep in v1_original["endpoints"]:
        if ep["response"]:
            ep["response"]["properties"]["legacy_score"] = {"type": "string"}
    write_snapshots(app, registry, tmp_path)  # 先写一份正常基线
    (tmp_path / "contract-v1.json").write_text(render_json(v1_original), encoding="utf-8")

    with pytest.raises(SnapshotConflictError):
        write_snapshots(app, registry, tmp_path)
    with pytest.raises(BreakingContractChangeError):
        write_snapshots(app, registry, tmp_path, allow_update=True)
    write_snapshots(app, registry, tmp_path, allow_update=True, allow_breaking=True)
    final = json.loads((tmp_path / "contract-v1.json").read_text(encoding="utf-8"))
    assert "legacy_score" not in json.dumps(final, ensure_ascii=False)


def test_compatible_metadata_change_flows_with_allow_update(app_and_registry, tmp_path):
    logger.info("用例: 纯兼容的版本元数据变化 allow_update 即可落盘，不触发破坏门禁")
    app, registry = app_and_registry
    write_snapshots(app, registry, tmp_path)
    path = tmp_path / "contract-v1.json"
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["version"]["successor"] = "v3"  # 提示性元数据：兼容
    path.write_text(render_json(snap), encoding="utf-8")
    with pytest.raises(SnapshotConflictError):
        write_snapshots(app, registry, tmp_path)
    write_snapshots(app, registry, tmp_path, allow_update=True)


def test_diff_snapshot_against_disk(app_and_registry, tmp_path):
    logger.info("用例: diff_snapshot 读盘比对，与离线快照一致时无破坏")
    app, registry = app_and_registry
    write_snapshots(app, registry, tmp_path)
    report = diff_snapshot(app, registry, tmp_path, "v1")
    assert report.is_compatible and report.changes == []


# ----------------------------- 服务端点 ------------------------------------ #


def test_contract_diff_endpoint_reports_committed_snapshots(client):
    logger.info("用例: /contract/diff 返回各版本结构化差异，当前快照无漂移")
    body = client.get("/contract/diff").json()
    assert body["compatible_overall"] is True
    assert {r["version"] for r in body["reports"]} == {"v0", "v1", "v2"}
    for report in body["reports"]:
        assert {"version", "compatible", "breaking_count", "compatible_change_count",
                "changes", "snapshot_present"} <= set(report)


def test_contract_diff_version_text_endpoint(client):
    logger.info("用例: /contract/diff/v1?format=text 返回人可读报告")
    r = client.get("/contract/diff/v1", params={"format": "text"})
    assert r.status_code == 200
    assert "契约差异报告: v1" in r.text


def test_contract_diff_unknown_version_is_404(client):
    logger.info("用例: 未注册版本的差异报告返回 404")
    assert client.get("/contract/diff/v9").status_code == 404
