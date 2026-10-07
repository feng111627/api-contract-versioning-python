"""场景：运行时字段级废弃提醒（Deprecation-Info 响应头）。

- 请求里传了废弃字段：条目 direction=request，定位到具体 JSONPath；
- 响应里带出废弃字段：条目 direction=response；
- 条目讲清楚字段、计划下线日期、建议迁向、原因；
- 没用废弃字段的请求（且响应也不带出时）不出现提醒；
- 活跃版本 v2 永远不产生提醒；
- 提醒以响应头承载，不改变既有响应体（老契约字段集不回归）。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("tests.scenarios")


def _entries(response) -> list[dict]:
    return json.loads(response.headers["deprecation-info"])


def test_request_using_deprecated_field_gets_notice(client):
    logger.info("用例: v1 请求传入废弃字段 nickname -> 响应带 request 方向结构化提醒")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.status_code == 201
    entries = _entries(r)
    request_entries = [e for e in entries if e["direction"] == "request"]
    assert len(request_entries) == 1
    entry = request_entries[0]
    assert entry["field"] == "nickname"
    assert entry["locations"] == ["$.nickname"]
    assert entry["deprecated_since"] == "2026-01-15"
    assert entry["sunset_at"] == "2026-12-31"
    assert entry["successor_field"] == "v2.display_name"
    assert "display_name" in (entry["reason"] or "")


def test_response_carrying_deprecated_field_gets_notice(client):
    logger.info("用例: v1 响应带出废弃字段 nickname -> 响应带 response 方向提醒")
    r = client.post("/v1/users", json={"name": "Carol"})
    assert r.status_code == 201
    entries = _entries(r)
    response_entries = [e for e in entries if e["direction"] == "response"]
    assert len(response_entries) == 1
    assert response_entries[0]["field"] == "nickname"
    assert response_entries[0]["locations"] == ["$.nickname"]


def test_get_response_also_annotated(client):
    logger.info("用例: v1 GET 响应同样带废弃提醒")
    client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    r = client.get("/v1/users/1")
    assert r.status_code == 200
    assert any(e["direction"] == "response" and e["field"] == "nickname"
               for e in _entries(r))


def test_notice_header_lists_deprecated_fields(client):
    logger.info("用例: Deprecation-Fields 汇总头列出本次命中的废弃字段")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.headers["deprecation-fields"] == "nickname"


def test_active_version_never_annotated(client):
    logger.info("用例: 活跃版本 v2 即使请求/响应含同名字符串也不产生任何废弃提醒")
    r = client.post(
        "/v2/users",
        json={"name": "Alice", "display_name": "Al", "email": "a@example.com"},
    )
    assert r.status_code == 201
    assert "deprecation-info" not in r.headers
    assert "deprecation-fields" not in r.headers
    r = client.get("/v2/users/1")
    assert "deprecation-info" not in r.headers


def test_notice_does_not_change_response_body(client):
    logger.info("用例: 废弃提醒只落在响应头，响应体与 v1 既有契约逐字段一致")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.json() == {"id": 1, "name": "Bob", "nickname": "bobby"}
    client.post("/v1/users", json={"name": "Carol"})
    r2 = client.get("/v1/users/2")
    assert set(r2.json()) == {"id", "name", "nickname"}


def test_notice_only_when_deprecated_field_actually_used(client):
    logger.info("用例: 没有任何废弃字段参与时不带提醒（本服务 v1 响应恒定带出 nickname，"
                "故用错误请求验证：422 不产出废弃提醒）")
    # name 非法 -> 422 错误信封不含 nickname 字段，也不应出现废弃提醒
    r = client.post("/v1/users", json={"name": ""})
    assert r.status_code == 422
    assert "deprecation-info" not in r.headers


def test_notice_payload_is_structured_and_ascii_safe(client):
    logger.info("用例: 提醒头是 JSON、排序确定、ASCII 安全，可被程序解析")
    r1 = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    r2 = client.post("/v1/users", json={"name": "Dan", "nickname": "danny"})
    # 结构键完整
    for entry in json.loads(r1.headers["deprecation-info"]):
        assert {
            "field", "direction", "locations", "deprecated_since",
            "sunset_at", "successor_field", "reason",
        } <= set(entry)
    # 同形态请求的头值确定性一致（locations 之外的部分相同）
    assert r1.headers["deprecation-fields"] == r2.headers["deprecation-fields"]
    r1.headers["deprecation-info"].encode("ascii")  # 不抛异常即 ASCII 安全


def test_deprecation_info_works_alongside_version_deprecation_headers(client):
    logger.info("用例: 字段级提醒与版本级 Deprecation/Sunset/Link 头同时存在")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.headers["Deprecation"] == "2026-01-15"
    assert r.headers["Sunset"] == "2026-12-31"
    assert 'rel="successor-version"' in r.headers["Link"]
    assert "deprecation-info" in {k.lower() for k in r.headers.keys()}
