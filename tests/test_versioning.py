"""版本兼容主流程：多版本共存、新旧调用方各取所需。"""

from __future__ import annotations

import re

import pytest


@pytest.mark.scenario("版本兼容")
class TestVersionCoexistence:
    """场景：多版本同时在线，老调用方与新调用方都能拿到预期结果。"""

    def test_v1_keeps_legacy_single_name_field(self, client) -> None:
        """v1 老调用方：仍然拿到单字段 name，且看不到 v2 新增的 tier。"""
        body = client.get("/v1/users/1").json()
        assert body == {"id": 1, "name": "Alice Wang", "email": "alice@example.com"}
        assert "tier" not in body
        assert "first_name" not in body

    def test_v2_returns_split_name_and_new_tier_field(self, client) -> None:
        """v2 新调用方：name 被拆分为 first_name/last_name，并拿到 tier 新字段。"""
        body = client.get("/v2/users/1").json()
        assert body["id"] == 1
        assert body["first_name"] == "Alice"
        assert body["last_name"] == "Wang"
        assert body["tier"] == "pro"

    def test_versions_share_same_underlying_data(self, client) -> None:
        """数据层共用：v1 创建的用户在 v2 立即可见（差异只在契约层）。"""
        created = client.post("/v1/users", json={"name": "Carol Chen"}).json()
        v2_view = client.get(f"/v2/users/{created['id']}").json()
        assert v2_view["first_name"] == "Carol"
        assert v2_view["last_name"] == "Chen"

    def test_v2_accepts_legacy_name_for_backward_compat(self, client) -> None:
        """兼容期：v2 仍接受 v1 风格的 name 入参并自动拆分。"""
        resp = client.post("/v2/users", json={"name": "Dan Zhao"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["first_name"] == "Dan"
        assert body["last_name"] == "Zhao"
        # 使用废弃入参时给出可机器识别的迁移提示
        assert resp.headers["Deprecation"] == "request-fields: name"

    def test_v2_rejects_name_and_split_fields_together(self, client) -> None:
        """边界：name 与 first_name/last_name 同时提供时按业务错误拒绝。"""
        resp = client.post(
            "/v2/users",
            json={"name": "Old Style", "first_name": "New", "last_name": "Style"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.scenario("版本废弃声明")
class TestDeprecationAnnouncements:
    """场景：被废弃的版本/字段/接口带出明确状态与计划下线时间。"""

    def test_deprecated_version_carries_standard_headers(self, client) -> None:
        """v1 响应带 Deprecation、Sunset 与后继版本 Link 头。"""
        resp = client.get("/v1/users")
        assert resp.status_code == 200
        assert resp.headers["X-Api-Version"] == "v1"
        assert re.match(r"[A-Za-z]{3}, \d{2} [A-Za-z]{3} \d{4}", resp.headers["Deprecation"])
        assert "Thu, 01 Apr 2027 00:00:00 GMT" == resp.headers["Sunset"]
        assert resp.headers["Link"] == '/v2; rel="successor-version"'

    def test_active_version_has_no_deprecation_header(self, client) -> None:
        """v2 整体仍 active：非废弃端点不应出现版本级 Deprecation 头。"""
        resp = client.get("/v2/users/1")
        assert resp.headers["X-Api-Version"] == "v2"
        # 字段级废弃提示仍保留
        assert resp.headers["Deprecation"] == "fields: email"

    def test_deprecated_endpoint_is_marked_in_contract(self, client) -> None:
        """接口级废弃：/v2/reports/legacy-summary 可正常访问但在文档中标记 deprecated。"""
        resp = client.get("/v2/reports/legacy-summary")
        assert resp.status_code == 200
        contract = client.get("/docs/contract").json()
        v2_section = next(v for v in contract["versions"] if v["version"]["name"] == "v2")
        legacy = next(
            e for e in v2_section["endpoints"] if "legacy-summary" in e["path"]
        )
        assert legacy["deprecated"] is True

    def test_deprecated_field_is_marked_in_schema(self, client) -> None:
        """字段级废弃：v2 响应模型的 email 在 schema 中带 deprecated 与 sunset。"""
        contract = client.get("/docs/contract").json()
        v2_section = next(v for v in contract["versions"] if v["version"]["name"] == "v2")
        endpoint = next(
            e for e in v2_section["endpoints"] if e["path"] == "/v2/users/{user_id}"
        )
        email = endpoint["response_schema"]["properties"]["email"]
        assert email["deprecated"] is True
        assert email["sunset"] == "2027-10-01"

    def test_version_metadata_exposes_sunset_in_index(self, client) -> None:
        """根自描述：每个版本的废弃状态与下线日期可查询。"""
        versions = {v["name"]: v for v in client.get("/").json()["versions"]}
        assert versions["v1"]["status"] == "deprecated"
        assert versions["v1"]["sunset"] == "2027-04-01"
        assert versions["v1"]["successor"] == "v2"
        assert versions["v2"]["status"] == "active"


@pytest.mark.scenario("版本下线与拒绝")
class TestRetirementRejection:
    """场景：访问已下线/未知版本得到一致且可区分的拒绝结果。"""

    def test_retired_version_returns_410_with_stable_code(self, client) -> None:
        """已下线 v0：任何端点都返回 410 + VERSION_RETIRED，而非普通 404。"""
        resp = client.get("/v0/users/123")
        assert resp.status_code == 410
        body = resp.json()
        assert body["error"]["code"] == "VERSION_RETIRED"
        assert "2025-01-01" in body["error"]["message"]
        assert body["error"]["details"] == {"successor": "v1"}

    def test_retired_version_rejected_on_any_method(self, client) -> None:
        """边界：下线版本无论 GET 还是 POST 都被一致拒绝。"""
        assert client.post("/v0/users", json={}).status_code == 410
        assert client.get("/v0/anything/deep").status_code == 410

    def test_unknown_version_is_distinguishable_from_unknown_route(self, client) -> None:
        """未知版本 v9 → 404 VERSION_UNKNOWN；已知版本下错误路径 → 404 RESOURCE_NOT_FOUND。"""
        unknown_version = client.get("/v9/users")
        unknown_route = client.get("/v2/does-not-exist")
        assert unknown_version.status_code == 404
        assert unknown_route.status_code == 404
        assert unknown_version.json()["error"]["code"] == "VERSION_UNKNOWN"
        assert unknown_route.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_non_versioned_paths_still_served(self, client) -> None:
        """边界：不带版本前缀的路径不受版本守卫影响。"""
        assert client.get("/health").status_code == 200
        assert client.get("/docs/contract").status_code == 200
