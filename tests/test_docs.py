"""文档自描述主流程：自动生成、确定性可复现、历史契约不被覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.docs import generate_contract, serialize_contract
from app.main import create_app
from app.main import build_registry


SNAPSHOT = Path(__file__).resolve().parent.parent / "docs" / "contract.json"


def _versions_of(contract: dict) -> dict[str, dict]:
    return {section["version"]["name"]: section for section in contract["versions"]}


@pytest.mark.scenario("文档自动生成与自描述")
class TestContractSelfDescribing:
    """场景：文档根据路由与模型自动生成，反映版本、废弃状态与统一错误结构。"""

    def test_contract_lists_every_registered_version(self, client) -> None:
        """三个版本（含已下线 v0）都出现在文档中。"""
        contract = client.get("/docs/contract").json()
        assert list(_versions_of(contract)) == ["v0", "v1", "v2"]

    def test_contract_reflects_deprecation_and_sunset(self, client) -> None:
        """版本级废弃状态、下线时间、后继版本在文档中完整可见。"""
        versions = _versions_of(client.get("/docs/contract").json())
        assert versions["v1"]["version"]["status"] == "deprecated"
        assert versions["v1"]["version"]["sunset"] == "2027-04-01"
        assert versions["v1"]["version"]["successor"] == "v2"
        assert versions["v0"]["version"]["status"] == "retired"
        assert versions["v0"]["retirement_policy"] == {
            "http_status": 410,
            "error_code": "VERSION_RETIRED",
        }

    def test_contract_documents_unified_error_structure(self, client) -> None:
        """统一错误信封 schema 与全部错误码都写在文档 shared 节。"""
        shared = client.get("/docs/contract").json()["shared"]
        assert "error" in shared["error_response_schema"]["properties"]
        codes = {entry["code"]: entry["http_status"] for entry in shared["error_codes"]}
        assert codes["VERSION_RETIRED"] == 410
        assert codes["VERSION_UNKNOWN"] == 404
        assert codes["VALIDATION_ERROR"] == 422
        assert codes["INTERNAL_ERROR"] == 500

    def test_contract_endpoints_match_registered_routes(self, client) -> None:
        """文档中的端点清单与实际路由一致：方法、路径、摘要均来自代码。"""
        versions = _versions_of(client.get("/docs/contract").json())
        v1_paths = {(e["path"], tuple(e["methods"])) for e in versions["v1"]["endpoints"]}
        assert ("/v1/users", ("GET",)) in v1_paths
        assert ("/v1/users", ("POST",)) in v1_paths
        assert ("/v1/users/{user_id}", ("GET",)) in v1_paths
        v2_paths = {(e["path"], tuple(e["methods"])) for e in versions["v2"]["endpoints"]}
        assert ("/v2/reports/legacy-summary", ("GET",)) in v2_paths


@pytest.mark.scenario("文档可复现")
class TestContractDeterminism:
    """场景：多次生成结果一致，并与落盘快照一致。"""

    def test_repeated_generation_is_byte_identical(self) -> None:
        """同一应用连续生成两次，序列化结果字节级相同。"""
        app = create_app()
        contract = generate_contract(app, build_registry())
        first = serialize_contract(contract)
        second = serialize_contract(generate_contract(app, build_registry()))
        assert first == second

    def test_generated_contract_matches_committed_snapshot(self, client) -> None:
        """当前代码生成结果必须与 docs/contract.json 完全一致（防漂移）。"""
        live = client.get("/docs/contract").json()
        committed = SNAPSHOT.read_text(encoding="utf-8")
        assert serialize_contract(live) == committed


@pytest.mark.scenario("历史契约保护")
class TestHistoricalContractPreserved:
    """场景：新版本演进不能悄悄覆盖历史版本契约。"""

    def test_v1_contract_keeps_legacy_shape(self, client) -> None:
        """v1 文档仍记录 name 单字段，不被 v2 的 first_name/last_name 覆盖。"""
        versions = _versions_of(client.get("/docs/contract").json())
        endpoint = next(
            e for e in versions["v1"]["endpoints"] if e["path"] == "/v1/users/{user_id}"
        )
        properties = endpoint["response_schema"]["properties"]
        assert "name" in properties
        assert "first_name" not in properties
        assert "tier" not in properties

    def test_v2_contract_keeps_evolved_shape(self, client) -> None:
        """v2 文档记录拆分字段与新增 tier，两套契约并存且互不覆盖。"""
        versions = _versions_of(client.get("/docs/contract").json())
        endpoint = next(
            e for e in versions["v2"]["endpoints"] if e["path"] == "/v2/users/{user_id}"
        )
        properties = endpoint["response_schema"]["properties"]
        assert {"first_name", "last_name", "tier", "email"} <= set(properties)
