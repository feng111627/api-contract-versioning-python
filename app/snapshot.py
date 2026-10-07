"""并发安全的契约快照写入。

串行化
------
- 进程内：按快照目录解析路径复用一把可重入锁（``SnapshotStore._dir_locks``），
  同一目录的并发生成/写入在进程内排队；
- 跨进程：目录内 ``.snapshots.lock`` 上的 ``flock`` 排他锁，多进程同时跑
  生成脚本也不会互相踩。

原子性
------
1. 锁内只生成一次契约（确定性输出，与单线程生成逐字节一致）；
2. 先对**所有**版本做历史保护与破坏性变更门禁校验，任一份不通过则全部不写，
   已存在的快照保持原样（整份成功或保持原样）；
3. 校验通过后，每份快照先写同目录临时文件、fsync 后 ``os.replace`` 原子替换，
   读侧任何时刻看到的都是完整的旧文件或完整的新文件，不会看到半截内容。

门禁规则
--------
- 快照不存在（新版本首次落盘）：允许写入；
- 内容相同：幂等，原样重写；
- 仅含兼容变化：默认仍拒绝覆盖（历史快照的任何修改都必须是有意的），
  传 ``allow_update=True`` 放行；
- 含破坏性变化：抛 ``BreakingChangeError``（同时也是 ``SnapshotConflictError``），
  必须显式 ``allow_breaking=True`` 才放行（该标志同时意味着允许更新）。
"""
from __future__ import annotations

import fcntl
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from app.diff import diff_version_contracts
from app.docs import (
    SnapshotConflictError,
    contract_for_version,
    generate_contract,
    render_json,
)
from app.versioning import VersionRegistry

_LOCKFILE_NAME = ".snapshots.lock"


class BreakingChangeError(SnapshotConflictError):
    """历史契约中检测到破坏性变更，默认拒绝写入。

    携带完整差异报告（``self.report``），供调用方展示/返回。
    """

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _atomic_write_text(path: Path, content: str) -> None:
    """同目录临时文件 + fsync + os.replace，保证原子可见。"""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


class SnapshotStore:
    """单个快照目录对应的写入器；同目录实例共享进程内锁。"""

    _dir_locks: dict[str, threading.RLock] = {}
    _dir_locks_guard = threading.Lock()

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        key = str(self.directory.resolve())
        with self._dir_locks_guard:
            lock = self._dir_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._dir_locks[key] = lock
        self._lock = lock

    def render_version_contents(
        self, app: FastAPI, registry: VersionRegistry
    ) -> dict[str, str]:
        """生成一次契约，返回每个版本确定性的快照文本。"""
        contract = generate_contract(app, registry)
        return {
            version: render_json(contract_for_version(contract, version))
            for version in [v.name for v in registry.all()]
        }

    def diff_on_disk(
        self,
        app: FastAPI,
        registry: VersionRegistry,
        version: str,
        content: str,
    ) -> dict[str, Any] | None:
        """与磁盘上已有的该版本快照对比；不存在则返回 None。"""
        import json

        path = self.directory / f"contract-{version}.json"
        if not path.exists():
            return None
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            old = None
        if not isinstance(old, dict) or not {"version", "endpoints", "errors"} <= set(old):
            # 历史文件缺失/损坏/不是契约：无法做兼容性分析，按普通历史冲突处理，
            # 由 allow_update（显式重写）放行，而不是伪装成破坏性变更。
            return None
        new = json.loads(content)
        return diff_version_contracts(old, new, version=version)

    def read_on_disk_contract(self) -> dict[str, Any] | None:
        """读取磁盘上各版本快照，拼回全量契约；目录为空时返回 None。"""
        import json

        versions: list[dict[str, Any]] = []
        endpoints: list[dict[str, Any]] = []
        errors: dict[str, Any] | None = None
        for path in sorted(self.directory.glob("contract-v*.json")):
            try:
                slice_ = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return None
            if not isinstance(slice_, dict) or "version" not in slice_:
                return None
            versions.append(slice_["version"])
            endpoints.extend(slice_.get("endpoints", []))
            if errors is None:
                errors = slice_.get("errors", {})
        if not versions:
            return None
        return {
            "versions": sorted(versions, key=lambda v: v["name"]),
            "endpoints": sorted(
                endpoints, key=lambda e: (e.get("version", ""), e.get("path", ""), e.get("method", ""))
            ),
            "errors": errors or {},
        }

    def diff_against_disk(self, app: FastAPI, registry: VersionRegistry) -> dict[str, Any]:
        """当前代码生成的契约 vs 磁盘历史快照的全量差异报告。"""
        from app.diff import diff_contracts

        generated = generate_contract(app, registry)
        on_disk = self.read_on_disk_contract()
        return diff_contracts(on_disk, generated)

    def write_all(
        self,
        app: FastAPI,
        registry: VersionRegistry,
        *,
        allow_update: bool = False,
        allow_breaking: bool = False,
    ) -> list[Path]:
        """串行化地整份校验并原子写入所有版本快照。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / _LOCKFILE_NAME
        with self._lock:
            with open(lock_path, "a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    return self._write_locked(
                        app,
                        registry,
                        allow_update=allow_update,
                        allow_breaking=allow_breaking,
                    )
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _write_locked(
        self,
        app: FastAPI,
        registry: VersionRegistry,
        *,
        allow_update: bool,
        allow_breaking: bool,
    ) -> list[Path]:
        contents = self.render_version_contents(app, registry)

        # 第一步：对所有版本整份校验，任何一个不通过都不写任何文件。
        pending: list[tuple[Path, str]] = []
        reports: dict[str, dict[str, Any]] = {}
        for version, content in contents.items():
            path = self.directory / f"contract-{version}.json"
            if not path.exists():
                pending.append((path, content))
                continue
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                pending.append((path, content))  # 幂等，字节不变
                continue
            report = self.diff_on_disk(app, registry, version, content)
            reports[version] = report
            if report is not None and report.get("has_breaking_changes"):
                if not allow_breaking:
                    raise BreakingChangeError(
                        "snapshot for "
                        f"{version} contains breaking changes against the committed "
                        "contract; refusing to overwrite history "
                        "(use allow_breaking to accept the change intentionally)",
                        report,
                    )
            elif not allow_update:
                raise SnapshotConflictError(
                    f"snapshot {path} differs from generated contract; "
                    "refusing to overwrite history (use allow_update to bump intentionally)"
                )
            pending.append((path, content))

        # 第二步：全部校验通过后逐个原子替换。
        written: list[Path] = []
        for path, content in pending:
            _atomic_write_text(path, content)
            written.append(path)
        return written
