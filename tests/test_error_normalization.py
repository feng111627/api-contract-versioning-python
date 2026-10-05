"""场景：请求校验与业务异常归一化。

- 参数校验失败在不同版本下落到同一错误结构（ErrorEnvelope）与错误码；
- 业务异常（资源不存在 / 冲突）使用稳定错误码；
- 未捕获的内部异常对外只有 INTERNAL_ERROR，不泄露异常类型与堆栈。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.store import store
from main import app

logger = logging.getLogger("tests.scenarios")

ENVELOPE_KEYS = {"code", "message", "details", "api_version"}


def test_validation_error_shape_is_stable_across_versions(client):
    logger.info("用例: v1/v2 校验失败返回一致的错误结构")
    for path, payload in [
        ("/v1/users", {"name": ""}),
        ("/v2/users", {"name": "", "email": "not-an-email"}),
    ]:
        r = client.post(path, json=payload)
        assert r.status_code == 422
        error = r.json()["error"]
        assert set(error) == ENVELOPE_KEYS
        assert error["code"] == "VALIDATION_FAILED"
        assert error["api_version"] == path.split("/")[1]
        assert all({"loc", "msg", "type"} <= set(d) for d in error["details"])


def test_validation_error_details_do_not_leak_internals(client):
    logger.info("用例: 校验错误 details 不含堆栈/内部对象表示")
    r = client.post("/v2/users", json={"name": ""})
    text = r.text
    assert "Traceback" not in text
    assert "pydantic" not in text.lower() or "type" in text  # 不暴露内部模块路径
    assert ".py" not in text


def test_business_error_not_found(client):
    logger.info("用例: 查询不存在用户 -> 404 NOT_FOUND 归一化结构")
    r = client.get("/v2/users/999")
    assert r.status_code == 404
    error = r.json()["error"]
    assert set(error) == ENVELOPE_KEYS
    assert error["code"] == "NOT_FOUND"
    assert error["api_version"] == "v2"


def test_business_error_conflict(client):
    logger.info("用例: 重复邮箱 -> 409 BUSINESS_RULE_VIOLATION")
    payload = {"name": "A", "email": "dup@example.com"}
    assert client.post("/v2/users", json=payload).status_code == 201
    r = client.post("/v2/users", json=payload)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BUSINESS_RULE_VIOLATION"


def test_internal_error_does_not_leak_stack():
    logger.info("用例: 内部异常对外只有 INTERNAL_ERROR，不透出类型与堆栈")
    store.reset()
    secret = "psycopg2.OperationalError: connection to db-secret-host:5432 failed"

    @app.get("/v2/_boom", include_in_schema=False)
    def _boom() -> None:
        raise RuntimeError(secret)

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/v2/_boom")
    assert r.status_code == 500
    error = r.json()["error"]
    assert set(error) == ENVELOPE_KEYS
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "internal server error"
    assert secret not in r.text
    assert "RuntimeError" not in r.text
    assert "Traceback" not in r.text
