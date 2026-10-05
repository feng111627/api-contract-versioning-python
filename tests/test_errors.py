"""异常归一化主流程：校验失败、业务异常、内部异常都落到统一结构与稳定错误码。"""

from __future__ import annotations

import pytest


@pytest.mark.scenario("校验错误归一化")
class TestValidationNormalization:
    """场景：不同版本的参数错误/校验失败落到同一错误结构。"""

    def test_missing_required_field_uses_validation_code(self, client) -> None:
        """v1 缺必填字段：422 + VALIDATION_ERROR，details 指出具体字段。"""
        resp = client.post("/v1/users", json={})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        fields = {item["field"] for item in body["error"]["details"]}
        assert "name" in fields

    def test_constraint_violation_normalized_across_versions(self, client) -> None:
        """边界：v1 长度约束与 v2 正则约束失败，结构完全一致、码值一致。"""
        v1_resp = client.post("/v1/users", json={"name": ""})
        v2_resp = client.post(
            "/v2/users",
            json={"first_name": "A", "last_name": "B", "tier": "vip"},
        )
        assert v1_resp.status_code == v2_resp.status_code == 422
        for resp in (v1_resp, v2_resp):
            error = resp.json()["error"]
            assert error["code"] == "VALIDATION_ERROR"
            assert isinstance(error["details"], list)
            assert error["details"][0].keys() == {"field", "issue"}

    def test_wrong_type_body_is_normalized(self, client) -> None:
        """边界：请求体类型错误（数组而非对象）也走统一 422 信封。"""
        resp = client.post("/v1/users", json=["not", "an", "object"])
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_business_rule_conflict_uses_bad_request_code(self, client) -> None:
        """业务规则失败（validator 抛出的业务异常）→ 400 BAD_REQUEST。"""
        resp = client.post("/v2/users", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "BAD_REQUEST"

    def test_error_envelope_shape_is_stable(self, client) -> None:
        """边界：所有错误信封都只有 error.code / error.message，形态稳定。"""
        responses = [
            client.post("/v1/users", json={}),
            client.get("/v2/users/999"),
            client.get("/v9/x"),
            client.get("/v0/x"),
        ]
        for resp in responses:
            top_keys = set(resp.json().keys())
            assert top_keys == {"error"}
            error_keys = set(resp.json()["error"].keys())
            assert {"code", "message"} <= error_keys
            assert error_keys <= {"code", "message", "details"}


@pytest.mark.scenario("业务异常归一化")
class TestBusinessErrorNormalization:
    """场景：资源缺失等业务异常映射为稳定错误码。"""

    def test_missing_resource_same_code_in_both_versions(self, client) -> None:
        """同一业务问题在 v1/v2 下返回相同错误码 RESOURCE_NOT_FOUND。"""
        v1_resp = client.get("/v1/users/999")
        v2_resp = client.get("/v2/users/999")
        assert v1_resp.status_code == v2_resp.status_code == 404
        assert v1_resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert v2_resp.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_method_not_allowed_is_normalized(self, client) -> None:
        """边界：405 也归一化为统一信封而非框架默认 detail。"""
        resp = client.delete("/v2/users/1")
        assert resp.status_code == 405
        assert resp.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.scenario("内部异常防泄漏")
class TestInternalErrorMasking:
    """场景：未预期内部异常对外只暴露固定结构，绝不透出类型与堆栈。"""

    def test_unexpected_error_returns_generic_envelope(self) -> None:
        """未捕获异常：500 + INTERNAL_ERROR 固定文案。"""
        from fastapi.testclient import TestClient

        from app.main import create_app

        app = create_app()

        @app.get("/v2/_boom_500")
        def boom() -> dict:
            raise RuntimeError("INTERNAL SECRET token=abcd1234 host=db-01.internal")

        resp = TestClient(app, raise_server_exceptions=False).get("/v2/_boom_500")
        assert resp.status_code == 500
        assert resp.json() == {
            "error": {"code": "INTERNAL_ERROR", "message": "服务内部错误，请稍后重试。"}
        }

    def test_response_body_never_contains_internals(self) -> None:
        """边界：响应体中不得出现异常类名、堆栈帧、密钥或内网主机信息。"""
        from fastapi.testclient import TestClient

        from app.main import create_app

        app = create_app()

        @app.get("/v2/_boom_leak")
        def boom_leak() -> dict:
            raise KeyError("super-secret-key")

        body = TestClient(app, raise_server_exceptions=False).get("/v2/_boom_leak").text
        for forbidden in ("Traceback", "KeyError", "super-secret-key", "File \""):
            assert forbidden not in body
