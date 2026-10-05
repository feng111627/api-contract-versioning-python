"""场景：版本下线与未知版本的拒绝语义。

- 已下线版本 v0：一律 410 + VERSION_SUNSET，带下线时间与后继版本；
- 未注册版本 v9：400 + VERSION_UNKNOWN，与路由不存在（404）可区分；
- 已注册版本下不存在的路径：仍是普通 404。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("tests.scenarios")


def test_sunset_version_rejected_consistently(client):
    logger.info("用例: v0 任意路径与方法都被一致拒绝")
    for method, path in [("get", "/v0/users/1"), ("post", "/v0/users"), ("get", "/v0/anything")]:
        kwargs = {"json": {}} if method == "post" else {}
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 410, (method, path, r.status_code)
        body = r.json()
        assert body["error"]["code"] == "VERSION_SUNSET"
        assert body["error"]["api_version"] == "v0"


def test_sunset_response_explains_plan(client):
    logger.info("用例: 410 响应带 sunset_at 与 successor 说明")
    r = client.get("/v0/users/1")
    details = {d["field"]: d["value"] for d in r.json()["error"]["details"]}
    assert details["sunset_at"] == "2026-01-01"
    assert details["successor"] == "v2"


def test_unknown_version_is_not_404(client):
    logger.info("用例: 未注册版本 v9 返回 400 VERSION_UNKNOWN 而非 404")
    r = client.get("/v9/users/1")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VERSION_UNKNOWN"
    assert r.json()["error"]["api_version"] == "v9"


def test_unknown_path_in_known_version_is_plain_404(client):
    logger.info("用例: 已注册版本下的未知路径仍是普通 404（可区分）")
    r = client.get("/v2/no-such-resource")
    assert r.status_code == 404
