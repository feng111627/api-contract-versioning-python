"""场景：契约差异报告与兼容性分类。

- 逐条变化点区分 compatible / breaking，破坏性变更说明破坏位置；
- 新增端点、新增可选请求字段、响应新增字段、约束放宽、标记废弃属于兼容；
- 移除端点/字段、新增必填请求字段、收紧校验、响应字段可空化、版本进入下线、
  删除错误码属于破坏性；
- 报告本身是确定性、可被程序解析的结构。
"""
from __future__ import annotations

import copy
import json
import logging

from app.docs import generate_contract
from app.diff import BREAKING, COMPATIBLE, diff_contracts, diff_version_contracts

logger = logging.getLogger("tests.scenarios")


def _base() -> dict:
    return {
        "version": {
            "name": "v1",
            "status": "deprecated",
            "deprecated_since": "2026-01-15",
            "sunset_at": "2026-12-31",
            "successor": "v2",
            "deprecated_fields": {},
        },
        "endpoints": [
            {
                "version": "v1",
                "method": "POST",
                "path": "/v1/users",
                "summary": "create_user",
                "request": {
                    "type": "object",
                    "title": "UserCreateV1",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 50},
                        "nickname": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                        },
                    },
                },
                "response": {
                    "type": "object",
                    "title": "UserV1",
                    "required": ["id", "name"],
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                },
            }
        ],
        "errors": {
            "codes": ["NOT_FOUND", "VALIDATION_FAILED"],
            "envelope": {"title": "ErrorEnvelope", "type": "object"},
        },
    }


def _kinds(report: dict, category: str) -> set[str]:
    return {c["kind"] for c in report["changes"] if c["category"] == category}


def _by_path(report: dict) -> dict[str, str]:
    return {c["path"]: c["category"] for c in report["changes"]}


def test_identical_contracts_have_no_changes(app_and_registry):
    logger.info("用例: 相同契约对比无变化点")
    app, registry = app_and_registry
    contract = generate_contract(app, registry)
    report = diff_contracts(contract, contract)
    assert report["has_changes"] is False
    assert report["has_breaking_changes"] is False
    assert report["breaking_change_count"] == 0


def test_compatible_changes_are_classified():
    logger.info("用例: 新增端点/可选请求字段/响应字段/约束放宽/标记废弃 判为兼容")
    old = _base()
    new = copy.deepcopy(old)
    new["endpoints"][0]["request"]["properties"]["bio"] = {
        "anyOf": [{"type": "string"}, {"type": "null"}]
    }
    new["endpoints"][0]["response"]["properties"]["extra"] = {"type": "string"}
    new["endpoints"][0]["request"]["properties"]["name"]["maxLength"] = 80
    new["endpoints"][0]["request"]["properties"]["nickname"]["deprecated"] = True
    new["endpoints"].append(
        {
            "version": "v1",
            "method": "GET",
            "path": "/v1/things",
            "summary": "list_things",
            "request": None,
            "response": {"type": "object", "properties": {}},
        }
    )
    report = diff_version_contracts(old, new, version="v1")
    assert report["has_breaking_changes"] is False
    assert _kinds(report, COMPATIBLE) == {
        "endpoint_added",
        "request_field_added_optional",
        "response_field_added",
        "request_constraint_loosened",
        "field_deprecated",
    }


def test_breaking_request_changes_are_flagged():
    logger.info("用例: 删请求字段/新增必填字段/收紧校验/字段变必填 判为破坏并定位字段")
    old = _base()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["request"]["properties"]["nickname"]
    new["endpoints"][0]["request"]["properties"]["name"]["minLength"] = 3
    new["endpoints"][0]["request"]["properties"]["age"] = {"type": "integer"}
    new["endpoints"][0]["request"]["required"] = ["name", "age"]
    report = diff_version_contracts(old, new, version="v1")
    assert report["breaking_change_count"] == 3
    assert _kinds(report, BREAKING) == {
        "request_field_removed",
        "request_constraint_tightened",
        "request_field_added_required",
    }
    paths = _by_path(report)
    assert any("request.nickname" in p and paths[p] == BREAKING for p in paths)
    removed = next(c for c in report["changes"] if c["kind"] == "request_field_removed")
    assert removed["after"] is None and removed["before"] is not None


def test_breaking_response_changes_are_flagged():
    logger.info("用例: 响应删字段/字段可空化 判为破坏；新增响应字段为兼容")
    old = _base()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["response"]["properties"]["name"]
    new["endpoints"][0]["response"]["properties"]["id"] = {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }
    new["endpoints"][0]["response"]["required"] = []
    new["endpoints"][0]["response"]["properties"]["badge"] = {"type": "string"}
    report = diff_version_contracts(old, new, version="v1")
    assert "response_field_removed" in _kinds(report, BREAKING)
    assert "response_field_type_changed" in _kinds(report, BREAKING)
    assert "response_field_added" in _kinds(report, COMPATIBLE)
    message = " ".join(c["message"] for c in report["changes"] if c["category"] == BREAKING)
    assert "老调用方" in message


def test_endpoint_removal_is_breaking():
    logger.info("用例: 端点被移除判为破坏，新增端点为兼容")
    old = _base()
    new = copy.deepcopy(old)
    new["endpoints"] = []
    report = diff_version_contracts(old, new, version="v1")
    assert _kinds(report, BREAKING) == {"endpoint_removed"}


def test_version_lifecycle_diffs():
    logger.info("用例: 版本进入下线/下线提前为破坏，下线推迟与废弃信息变化为兼容")
    old = _base()
    sunset = copy.deepcopy(old)
    sunset["version"]["status"] = "sunset"
    report = diff_version_contracts(old, sunset, version="v1")
    assert _kinds(report, BREAKING) == {"version_status_changed"}

    earlier = copy.deepcopy(old)
    earlier["version"]["sunset_at"] = "2026-06-30"
    report = diff_version_contracts(old, earlier, version="v1")
    assert _kinds(report, BREAKING) == {"sunset_date_moved_earlier"}

    later = copy.deepcopy(old)
    later["version"]["sunset_at"] = "2027-12-31"
    report = diff_version_contracts(old, later, version="v1")
    assert report["has_breaking_changes"] is False
    assert "sunset_date_moved_later" in _kinds(report, COMPATIBLE)


def test_error_code_removal_is_breaking():
    logger.info("用例: 删除错误码为破坏，新增错误码为兼容")
    old = _base()
    new = copy.deepcopy(old)
    new["errors"]["codes"] = ["INTERNAL_ERROR", "NOT_FOUND"]
    report = diff_version_contracts(old, new, version="v1")
    assert "error_code_removed" in _kinds(report, BREAKING)
    assert "error_code_added" in _kinds(report, COMPATIBLE)


def test_report_is_machine_parseable_and_deterministic():
    logger.info("用例: 差异报告可序列化、重复对比结果逐字节一致")
    old = _base()
    new = copy.deepcopy(old)
    del new["endpoints"][0]["request"]["properties"]["nickname"]
    r1 = json.dumps(diff_version_contracts(old, new, version="v1"), sort_keys=True)
    r2 = json.dumps(diff_version_contracts(old, new, version="v1"), sort_keys=True)
    parsed = json.loads(r1)
    assert r1 == r2
    assert all(
        set(c) == {"kind", "category", "path", "message", "before", "after"}
        for c in parsed["changes"]
    )
