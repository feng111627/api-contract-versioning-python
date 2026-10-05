"""测试公共夹具与场景化日志配置。

运行 ``pytest`` 时每个测试都会打印其覆盖的业务场景（类/函数 docstring），
便于从日志直接看出覆盖了哪些主流程与边界。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import create_app


def pytest_configure(config: pytest.Config) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [场景日志] %(message)s",
        force=True,
    )


@pytest.fixture(autouse=True)
def _log_scenario(request: pytest.FixtureRequest) -> Iterator[None]:
    store.reset_for_tests()
    scenario = (
        request.node.cls.__doc__.strip() if request.node.cls and request.node.cls.__doc__ else ""
    )
    case_doc = (request.function.__doc__ or "").strip()
    logging.getLogger("tests").info(
        "场景=%s | 用例=%s%s",
        scenario or request.module.__name__,
        request.node.name,
        f" | 说明={case_doc}" if case_doc else "",
    )
    yield


@pytest.fixture()
def client() -> TestClient:
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)
