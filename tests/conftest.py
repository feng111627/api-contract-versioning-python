from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.store import store
from main import app, registry

logger = logging.getLogger("tests.scenarios")


@pytest.fixture()
def client() -> TestClient:
    store.reset()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def app_and_registry():
    return app, registry


def pytest_runtest_logstart(nodeid: str, location: tuple) -> None:
    logger.info("▶ 场景开始: %s", nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        mark = {"passed": "✓ 通过", "failed": "✗ 失败", "skipped": "- 跳过"}[report.outcome]
        logger.info("■ 场景结束: %s => %s", report.nodeid, mark)
