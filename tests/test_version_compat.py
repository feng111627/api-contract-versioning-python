"""场景：多版本并存与向后兼容。

- v1（已废弃）旧调用方继续可用，响应带废弃提示头；
- v2（活跃）新字段正常工作；
- v1 的废弃字段 nickname 仍被接受（不破坏旧调用方）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tests.scenarios")


def test_deprecated_v1_still_usable(client):
    logger.info("用例: 废弃版本 v1 创建/查询用户仍返回 2xx")
    r = client.post("/v1/users", json={"name": "Bob", "nickname": "bobby"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "name": "Bob", "nickname": "bobby"}
    r = client.get("/v1/users/1")
    assert r.status_code == 200
    assert r.json()["nickname"] == "bobby"


def test_deprecated_v1_carries_deprecation_headers(client):
    logger.info("用例: v1 响应带 Deprecation/Sunset/Link 提示头")
    r = client.post("/v1/users", json={"name": "Bob"})
    assert r.headers["Deprecation"] == "2026-01-15"
    assert r.headers["Sunset"] == "2026-12-31"
    assert 'rel="successor-version"' in r.headers["Link"]
    assert "/v2/" in r.headers["Link"]


def test_active_v2_has_no_deprecation_headers(client):
    logger.info("用例: 活跃版本 v2 不带废弃提示头")
    r = client.post("/v2/users", json={"name": "Alice", "email": "a@example.com"})
    assert r.status_code == 201
    assert "Deprecation" not in r.headers
    assert "Sunset" not in r.headers


def test_v2_new_fields_roundtrip(client):
    logger.info("用例: v2 新字段 display_name/email 正常读写")
    r = client.post(
        "/v2/users",
        json={"name": "Alice", "display_name": "Al", "email": "a@example.com"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["display_name"] == "Al"
    assert body["email"] == "a@example.com"
    r = client.get(f"/v2/users/{body['id']}")
    assert r.json()["email"] == "a@example.com"


def test_versions_are_isolated(client):
    logger.info("用例: 同一资源在 v1/v2 下各自返回符合其契约的字段集")
    client.post("/v2/users", json={"name": "Alice", "email": "a@example.com"})
    v1_body = client.get("/v1/users/1").json()
    v2_body = client.get("/v2/users/1").json()
    assert set(v1_body) == {"id", "name", "nickname"}
    assert set(v2_body) == {"id", "name", "display_name", "email"}
