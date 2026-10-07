"""场景：运行时字段级废弃提醒（Deprecation-Notice 响应头）。

- 请求传入已废弃字段时，响应头逐字段给出下线日期与迁移建议；
- 响应带出已废弃字段的实际值时同样提醒；
- 同一次请求的请求/响应两侧都用到时两条都在；
- 活跃版本 v2 不带任何废弃提醒（也不改变响应体）；
- 不传/未带出废弃字段时不产生提醒；显式 null 的响应字段不算“还在用”；
- 非 JSON 响应与 v0 下线拒绝不受影响。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("tests.scenarios")

NOTICE_HEADER = "Deprecation-Notice"


def _notices(response) -> list[dict]:
    raw = response.headers.get(NOTICE_HEADER)
    assert raw is not None, "缺少 Deprecation-Notice 响应头"
    entries = json.loads(raw)
    assert isinstance(entries, list) and entries
    for entry in entries:
        assert {
            "field",
            "direction",
            "deprecated_since",
            "sunset_at",
            "replacement",
            "reason",
        } <= set(entry)
    return entries


def test_request_using_deprecated_field_gets_notice(client):
    logger.info("用例: v1 请求传入 nickname -> 结构化提醒字段/下线日/迁移目标")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.status_code == 201
    entries = _notices(r)
    req_entries = [e for e in entries if e["direction"] == "request" and e["field"] == "nickname"]
    assert len(req_entries) == 1
    entry = req_entries[0]
    assert entry["deprecated_since"] == "2026-01-15"
    assert entry["sunset_at"] == "2026-12-31"
    assert entry["replacement"] == "v2.display_name"
    assert entry["reason"]
    # 响应体与既有版本级废弃头都不受影响
    assert r.json() == {"id": 1, "name": "Bob", "nickname": "bobby"}
    assert r.headers["Deprecation"] == "2026-01-15"


def test_response_carrying_deprecated_field_gets_notice(client):
    logger.info("用例: v1 响应带出 nickname 实际值 -> 响应侧提醒")
    client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    r = client.get("/v1/users/1")
    assert r.status_code == 200
    entries = _notices(r)
    assert [e for e in entries if e["direction"] == "response" and e["field"] == "nickname"]


def test_request_and_response_both_reported(client):
    logger.info("用例: 请求与响应都用到废弃字段时，两条方向记录都在且去重")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    entries = _notices(r)
    pairs = sorted((e["field"], e["direction"]) for e in entries)
    assert pairs == [("nickname", "request"), ("nickname", "response")]


def test_no_notice_when_deprecated_field_unused(client):
    logger.info("用例: v1 不传 nickname 且响应仅返回 null -> 不产生字段级提醒")
    r = client.post("/v1/users", json={"name": "Carol"})
    assert r.status_code == 201
    assert NOTICE_HEADER not in r.headers
    r = client.get(f"/v1/users/{r.json()['id']}")
    assert NOTICE_HEADER not in r.headers


def test_active_version_never_carries_field_notice(client):
    logger.info("用例: 活跃版本 v2 任何请求都不带废弃提醒")
    r = client.post(
        "/v2/users",
        json={"name": "Alice", "display_name": "Al", "email": "a@example.com"},
    )
    assert r.status_code == 201
    assert NOTICE_HEADER not in r.headers
    assert "Deprecation" not in r.headers
    r = client.get("/v2/users/1")
    assert NOTICE_HEADER not in r.headers


def test_notice_header_is_structured_json(client):
    logger.info("用例: 提醒头可被程序解析，多字段时为结构化数组")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    entries = json.loads(r.headers[NOTICE_HEADER])
    assert all(e["sunset_at"] >= "2026-01-15" for e in entries)


def test_validation_error_response_still_normalized(client):
    logger.info("用例: 带废弃字段的请求校验失败时，错误结构仍归一且不串台")
    r = client.post("/v1/users", json={"name": "", "nickname": "bobby"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_FAILED"


def test_sunset_version_notice_behavior_unchanged(client):
    logger.info("用例: v0 下线拒绝不受字段提醒逻辑影响")
    r = client.get("/v0/users/1")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "VERSION_SUNSET"
    assert NOTICE_HEADER not in r.headers
