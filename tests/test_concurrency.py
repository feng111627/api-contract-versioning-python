"""场景：并发生成与快照写入的一致性、原子性与历史保护。

- 多份生成同时跑：结果与单线程逐字节一致（生成本身确定性）；
- 多份写入同时跑：最终文件与单次写入逐字节一致，无临时文件残留；
- 并发写入过程中任何时刻读到的快照都是完整 JSON（无半截文件）；
- 历史保护在并发下继续生效：破坏性漂移时并发写入要么全部被门禁拒绝、
  磁盘保持被保护的原内容，要么显式放行后整批一致。
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from app.docs import (
    BreakingContractChangeError,
    SnapshotConflictError,
    _atomic_write_text,
    contract_for_version,
    generate_contract,
    render_json,
    write_snapshots,
)

logger = logging.getLogger("tests.scenarios")


def test_concurrent_generation_is_byte_identical(app_and_registry):
    logger.info("用例: 16 个线程并发生成，全部与单线程结果逐字节一致")
    app, registry = app_and_registry
    expected = render_json(generate_contract(app, registry))
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(render_json(generate_contract(app, registry)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 16
    assert all(r == expected for r in results)


def test_concurrent_writes_leave_single_consistent_result(app_and_registry, tmp_path):
    logger.info("用例: 16 线程并发写同一快照目录，最终结果与单次写入逐字节一致")
    app, registry = app_and_registry
    expected: dict[str, str] = {}
    contract = generate_contract(app, registry)
    for v in [i.name for i in registry.all()]:
        expected[v] = render_json(contract_for_version(contract, v))

    def worker() -> list[Path]:
        return write_snapshots(app, registry, tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(16)]
        paths = [f.result() for f in as_completed(futures)]
    assert all(len(p) == len(expected) for p in paths)

    for version, content in expected.items():
        assert (tmp_path / f"contract-{version}.json").read_text(encoding="utf-8") == content
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"发现临时文件残留: {leftovers}"


def test_snapshot_files_always_complete_json_during_concurrent_writes(
    app_and_registry, tmp_path
):
    logger.info("用例: 并发写入进行中反复读取，任何快照都必须是完整可解析的 JSON")
    app, registry = app_and_registry
    stop = threading.Event()
    parse_failures: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            for path in tmp_path.glob("contract-*.json"):
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, FileNotFoundError, UnicodeDecodeError) as exc:
                    parse_failures.append(f"{path.name}: {exc}")

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: write_snapshots(app, registry, tmp_path), range(12)))
    finally:
        stop.set()
        for t in readers:
            t.join()
    assert parse_failures == [], parse_failures[:3]


def test_atomic_write_replaces_whole_file_or_keeps_original(tmp_path):
    logger.info("用例: 原子写直接验证：替换后是新整份内容，临时文件不留存")
    target = tmp_path / "contract-v1.json"
    target.write_text('{"v": 1}\n', encoding="utf-8")
    _atomic_write_text(target, '{"v": 2}\n')
    assert target.read_text(encoding="utf-8") == '{"v": 2}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_history_protection_holds_under_concurrent_writes(app_and_registry, tmp_path):
    logger.info("用例: 历史快照被破坏性漂移后，并发写入全部被门禁拒绝且磁盘原样保留")
    app, registry = app_and_registry
    write_snapshots(app, registry, tmp_path)
    victim = tmp_path / "contract-v1.json"
    protected = victim.read_text(encoding="utf-8")

    # 历史契约多出一个新代码不再产出的响应字段 -> 代码相对历史是破坏性删除
    snap = json.loads(protected)
    for ep in snap["endpoints"]:
        if ep["response"]:
            ep["response"]["properties"]["legacy_score"] = {"type": "string"}
    victim.write_text(render_json(snap), encoding="utf-8")
    drifted = victim.read_text(encoding="utf-8")

    outcomes: list[str] = []

    def worker() -> None:
        try:
            write_snapshots(app, registry, tmp_path, allow_update=True)
            outcomes.append("written")
        except BreakingContractChangeError:
            outcomes.append("blocked-breaking")
        except SnapshotConflictError:
            outcomes.append("blocked-conflict")

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes, "至少应有执行结果"
    assert set(outcomes) <= {"blocked-breaking", "blocked-conflict"}, outcomes
    # 被保护的历史内容原样保留
    assert victim.read_text(encoding="utf-8") == drifted


def test_concurrent_allowed_breaking_writes_converge(app_and_registry, tmp_path):
    logger.info("用例: 显式放行破坏性变更后，并发写入最终收敛为同一份新契约")
    app, registry = app_and_registry
    write_snapshots(app, registry, tmp_path)
    victim = tmp_path / "contract-v1.json"
    snap = json.loads(victim.read_text(encoding="utf-8"))
    for ep in snap["endpoints"]:
        if ep["response"]:
            ep["response"]["properties"]["legacy_score"] = {"type": "string"}
    victim.write_text(render_json(snap), encoding="utf-8")

    def worker() -> None:
        write_snapshots(
            app, registry, tmp_path, allow_update=True, allow_breaking=True
        )

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = render_json(contract_for_version(generate_contract(app, registry), "v1"))
    assert victim.read_text(encoding="utf-8") == expected
    assert "legacy_score" not in victim.read_text(encoding="utf-8")
