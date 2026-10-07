"""场景：契约快照的破坏性门禁与并发一致性/原子性。

- 默认拦截破坏性变更（BreakingChangeError 携带差异报告），allow_breaking 显式放行；
- 仅兼容变化允许在 allow_update 下放行；
- 多线程并发写：结果与单次生成逐字节一致，且每份文件始终是完整合法的 JSON；
- 跨进程并发写（subprocess）同样不互踩；
- 一份不通过则全部不写（整份原子，已存在快照保持原样）。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.docs import (
    SnapshotConflictError,
    contract_for_version,
    generate_contract,
    render_json,
    write_snapshots,
)
from app.snapshot import BreakingChangeError, SnapshotStore, _atomic_write_text
from app.versioning import VersionInfo, VersionRegistry, VersionStatus
from tests._scenario_models import (
    ReqV1,
    ReqV1Breaking,
    ReqV1Compat,
    RespV1,
    RespV1Breaking,
)

logger = logging.getLogger("tests.scenarios")


def _make_app(*, include_get: bool = True) -> FastAPI:
    """构造含具体模型类的 v1 测试应用。

    通过模块级工厂返回模型，避免闭包里 payload: <变量> 的注解无法被 FastAPI 解析。
    """
    from fastapi import APIRouter

    from tests._scenario_models import (
    ReqV1,
    ReqV1Breaking,
    ReqV1Compat,
    RespV1,
    RespV1Breaking,
)

    app = FastAPI()
    router = APIRouter(prefix="/v1")

    @router.post("/users", response_model=RespV1, status_code=201)
    def create_user(payload: ReqV1) -> dict:
        return {}

    if include_get:

        @router.get("/things", response_model=RespV1)
        def list_things() -> dict:
            return {}

    app.include_router(router)
    return app


def _make_app_custom(req_model, resp_model, *, include_get: bool = True) -> FastAPI:
    from fastapi import APIRouter

    app = FastAPI()
    router = APIRouter(prefix="/v1")

    def create_user(payload):  # noqa: ANN001
        return {}

    create_user.__annotations__["payload"] = req_model
    router.add_api_route(
        "/users", create_user, methods=["POST"],
        response_model=resp_model, status_code=201,
    )

    if include_get:

        def list_things() -> dict:
            return {}

        router.add_api_route(
            "/things", list_things, methods=["GET"], response_model=resp_model
        )

    app.include_router(router)
    return app


def _registry() -> VersionRegistry:
    from datetime import date

    reg = VersionRegistry()
    reg.register(
        VersionInfo(
            name="v1",
            status=VersionStatus.DEPRECATED,
            deprecated_since=date(2026, 1, 1),
        )
    )
    return reg


def test_breaking_change_blocked_by_default(tmp_path):
    logger.info("用例: 删除请求字段属于破坏性变更，默认拒绝写入并给出差异报告")
    app, reg = _make_app(), _registry()
    write_snapshots(app, reg, tmp_path)

    breaking_app = _make_app_custom(ReqV1Breaking, RespV1)
    with pytest.raises(BreakingChangeError) as exc:
        write_snapshots(breaking_app, reg, tmp_path)
    report = exc.value.report
    assert report["has_breaking_changes"] is True
    assert report["breaking_change_count"] >= 1
    assert any(c["kind"] == "request_field_removed" for c in report["changes"])
    # 磁盘上的历史快照保持原样
    old_text = (tmp_path / "contract-v1.json").read_text(encoding="utf-8")
    assert "nickname" in old_text


def test_breaking_change_requires_explicit_allow_breaking(tmp_path):
    logger.info("用例: allow_update 不能放行破坏性变更，必须显式 allow_breaking")
    app, reg = _make_app(), _registry()
    write_snapshots(app, reg, tmp_path)

    breaking_app = _make_app_custom(ReqV1, RespV1Breaking)
    with pytest.raises(BreakingChangeError):
        write_snapshots(breaking_app, reg, tmp_path, allow_update=True)
    written = write_snapshots(breaking_app, reg, tmp_path, allow_breaking=True)
    assert written == [tmp_path / "contract-v1.json"]
    snapshot = json.loads((tmp_path / "contract-v1.json").read_text(encoding="utf-8"))
    post_endpoint = next(e for e in snapshot["endpoints"] if e["method"] == "POST")
    assert "nickname" not in post_endpoint["response"]["properties"]
    # 请求字段 nickname 未改动，仍然保留
    assert "nickname" in post_endpoint["request"]["properties"]


def test_compatible_change_allowed_with_allow_update(tmp_path):
    logger.info("用例: 纯兼容变化（新增可选字段/新增端点）默认拒绝、allow_update 放行")
    app, reg = _make_app(), _registry()
    write_snapshots(app, reg, tmp_path)

    compat_app = _make_app_custom(ReqV1Compat, RespV1)
    with pytest.raises(SnapshotConflictError):
        write_snapshots(compat_app, reg, tmp_path)
    write_snapshots(compat_app, reg, tmp_path, allow_update=True)
    assert "bio" in (tmp_path / "contract-v1.json").read_text(encoding="utf-8")


def test_batch_is_all_or_nothing(tmp_path):
    logger.info("用例: 任一版本未通过门禁时，其它版本也不会被写入")
    from app.versioning import VersionInfo
    from datetime import date

    reg = VersionRegistry()
    reg.register(
        VersionInfo(name="v1", status=VersionStatus.DEPRECATED,
                    deprecated_since=date(2026, 1, 1))
    )
    reg.register(VersionInfo(name="v2", status=VersionStatus.ACTIVE))

    class ReqV2(BaseModel):
        name: str

    class RespV2(BaseModel):
        id: int

    app_v2_only = FastAPI()
    from fastapi import APIRouter

    router2 = APIRouter(prefix="/v2")

    @router2.post("/things", response_model=RespV2, status_code=201)
    def create_v2(payload: ReqV2) -> dict:
        return {}

    app_both = _make_app()
    app_both.include_router(router2)
    write_snapshots(app_both, reg, tmp_path)
    v1_before = (tmp_path / "contract-v1.json").read_text(encoding="utf-8")
    v2_before = (tmp_path / "contract-v2.json").read_text(encoding="utf-8")

    # v1 端点整体移除（破坏），同时 v2 保持不变：整批拒绝
    app_v2_only.include_router(
        __import__("fastapi", fromlist=["APIRouter"]).APIRouter(prefix="/v1")
    )
    with pytest.raises(BreakingChangeError):
        write_snapshots(app_v2_only, reg, tmp_path, allow_update=True)
    assert (tmp_path / "contract-v1.json").read_text(encoding="utf-8") == v1_before
    assert (tmp_path / "contract-v2.json").read_text(encoding="utf-8") == v2_before


def test_concurrent_writes_are_byte_identical(tmp_path):
    logger.info("用例: 16 线程并发生成写入，最终结果与单次生成逐字节一致")
    app, reg = _make_app(), _registry()
    store = SnapshotStore(tmp_path)
    expected = {
        "v1": render_json(
            contract_for_version(generate_contract(app, reg), "v1")
        )
    }

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: store.write_all(app, reg), range(16)))
    assert all(len(r) == 1 for r in results)
    assert (tmp_path / "contract-v1.json").read_text(encoding="utf-8") == expected["v1"]


def test_concurrent_readers_never_see_half_written_json(tmp_path):
    logger.info("用例: 并发读写过程中，读到的快照始终是完整合法的契约 JSON")
    app, reg = _make_app(), _registry()
    store = SnapshotStore(tmp_path)
    store.write_all(app, reg)
    stop = threading.Event()
    seen_invalid: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            path = tmp_path / "contract-v1.json"
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    assert data["version"]["name"] == "v1"
                except (ValueError, AssertionError) as exc:  # noqa: PERF203
                    seen_invalid.append(str(exc))

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for _ in range(40):
            store.write_all(app, reg)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert seen_invalid == []


def test_cross_process_concurrent_writes_are_consistent(tmp_path):
    logger.info("用例: 跨进程并发写（flock 串行化）结果仍与单次生成一致")
    app, reg = _make_app(), _registry()
    expected = render_json(contract_for_version(generate_contract(app, reg), "v1"))
    project = Path(__file__).resolve().parent.parent
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(project)!r})\n"
        "from app.snapshot import SnapshotStore\n"
        "from tests.test_snapshot_concurrency import _make_app, _registry\n"
        f"SnapshotStore({str(tmp_path)!r}).write_all(_make_app(), _registry())\n",
        encoding="utf-8",
    )
    procs = [
        subprocess.run(
            [sys.executable, str(worker)], cwd=project, capture_output=True, text=True
        )
        for _ in range(5)
    ]
    assert all(p.returncode == 0 for p in procs), [p.stderr for p in procs if p.returncode]
    assert (tmp_path / "contract-v1.json").read_text(encoding="utf-8") == expected


def test_atomic_write_helper_replaces_file_contents(tmp_path):
    logger.info("用例: 原子写助手先临时文件后替换，目标最终为完整新内容")
    path = tmp_path / "x.json"
    _atomic_write_text(path, '{"a": 1}\n')
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n'
