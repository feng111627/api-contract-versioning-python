"""场景：自描述契约文档。

- 契约由路由与模型自动生成，覆盖版本生命周期、端点模型与统一错误结构；
- 多次生成逐字节一致（可复现）；
- 快照按版本分文件，历史版本契约不会被悄悄覆盖；
- 服务暴露 /contract 自描述端点。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.docs import (
    SnapshotConflictError,
    contract_for_version,
    generate_contract,
    render_json,
    write_snapshots,
)

logger = logging.getLogger("tests.scenarios")

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


def test_contract_reflects_versions_and_lifecycle(app_and_registry):
    logger.info("用例: 契约包含各版本状态/废弃时间/计划下线/废弃字段")
    app, registry = app_and_registry
    contract = generate_contract(app, registry)
    versions = {v["name"]: v for v in contract["versions"]}
    assert versions["v2"]["status"] == "active"
    assert versions["v1"]["status"] == "deprecated"
    assert versions["v1"]["sunset_at"] == "2026-12-31"
    assert versions["v1"]["deprecated_fields"] == {"nickname": "2026-01-15"}
    assert versions["v0"]["status"] == "sunset"


def test_contract_reflects_endpoints_and_error_structure(app_and_registry):
    logger.info("用例: 契约包含各版本端点模型与统一错误结构")
    app, registry = app_and_registry
    contract = generate_contract(app, registry)
    endpoints = {(e["version"], e["method"], e["path"]) for e in contract["endpoints"]}
    assert ("v1", "POST", "/v1/users") in endpoints
    assert ("v2", "GET", "/v2/users/{user_id}") in endpoints
    assert "VERSION_SUNSET" in contract["errors"]["codes"]
    assert "INTERNAL_ERROR" in contract["errors"]["codes"]
    assert contract["errors"]["envelope"]["title"] == "ErrorEnvelope"


def test_generation_is_deterministic(app_and_registry):
    logger.info("用例: 连续多次生成结果逐字节一致")
    app, registry = app_and_registry
    first = render_json(generate_contract(app, registry))
    for _ in range(3):
        assert render_json(generate_contract(app, registry)) == first


def test_committed_snapshots_match_generation(app_and_registry):
    logger.info("用例: 仓库内快照与当前代码生成结果一致（未悄悄漂移）")
    app, registry = app_and_registry
    contract = generate_contract(app, registry)
    for version in [v.name for v in registry.all()]:
        path = CONTRACTS_DIR / f"contract-{version}.json"
        assert path.exists(), f"缺少快照 {path}"
        assert path.read_text(encoding="utf-8") == render_json(
            contract_for_version(contract, version)
        ), f"快照 {path} 与代码生成结果不一致"


def test_history_snapshot_not_silently_overwritten(app_and_registry, tmp_path):
    logger.info("用例: 历史快照内容变化时拒绝覆盖")
    app, registry = app_and_registry
    write_snapshots(app, registry, tmp_path)
    victim = tmp_path / "contract-v1.json"
    victim.write_text('{"tampered": true}\n', encoding="utf-8")
    with pytest.raises(SnapshotConflictError):
        write_snapshots(app, registry, tmp_path)
    # allow_update 用于有意升级契约
    write_snapshots(app, registry, tmp_path, allow_update=True)
    assert json.loads(victim.read_text(encoding="utf-8"))["version"]["name"] == "v1"


def test_contract_endpoint_serves_self_description(client):
    logger.info("用例: /contract 端点返回与离线生成一致的契约")
    body = client.get("/contract").json()
    assert [v["name"] for v in body["versions"]] == ["v0", "v1", "v2"]
    assert body["errors"]["codes"]


def test_contract_diff_endpoint_reports_committed_vs_generated(client):
    logger.info("用例: /contract/diff 返回历史快照与当前代码的结构化差异报告")
    body = client.get("/contract/diff").json()
    assert "versions" in body
    assert body["has_changes"] is False
    assert body["has_breaking_changes"] is False
    assert set(body["versions"]) == {"v0", "v1", "v2"}
    for name, report in body["versions"].items():
        assert report["version"] == name
        assert {"has_changes", "has_breaking_changes",
                "breaking_change_count", "compatible_change_count", "changes"} <= set(report)


def test_contract_diff_endpoint_version_filter(client):
    logger.info("用例: /contract/diff?version=v1 返回单版本切片，未知版本 404")
    r = client.get("/contract/diff?version=v1")
    assert r.status_code == 200
    assert r.json()["version"] == "v1"
    r = client.get("/contract/diff?version=v9")
    assert r.status_code == 404
