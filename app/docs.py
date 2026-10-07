"""自描述契约文档生成。

- 从 FastAPI 路由与 Pydantic 模型自动提取每个版本的端点与模型结构；
- 合并版本注册表中的生命周期元数据（废弃状态、计划下线时间、废弃字段）；
- 附带统一错误结构说明；
- 输出完全确定性：所有集合排序、JSON 序列化使用 sort_keys，
  不嵌入时间戳等易变数据，多次生成结果逐字节一致；
- 快照按版本分文件落盘（contracts/contract-<version>.json）：
  * 已存在的快照若内容变化，先用 app.diff 做兼容性比对；
  * 默认拒绝一切覆盖（allow_update 才允许兼容变更落盘）；
  * 破坏性变更即使 allow_update 也拒绝，必须显式 allow_breaking 放行；
  * 同一目录的生成/写入串行化，写入采用临时文件 + fsync + 原子 rename，
    不会出现半截文件，并发结果与单次生成逐字节一致。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.diff import ContractDiff, diff_version_contract
from app.errors import ErrorCode, ErrorEnvelope
from app.versioning import VersionInfo, VersionRegistry

# 仓库内离线快照的默认目录（服务端点与生成脚本共用同一份事实）。
CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


def _version_of(route: APIRoute) -> str | None:
    for part in route.path.strip("/").split("/"):
        if part.startswith("v") and part[1:].isdigit():
            return part
    return None


def _schema_of(model: Any) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_json_schema()


def _version_block(info: VersionInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "status": info.status.value,
        "deprecated_since": info.deprecated_since.isoformat() if info.deprecated_since else None,
        "sunset_at": info.sunset_at.isoformat() if info.sunset_at else None,
        "successor": info.successor,
        "deprecated_fields": {
            field: day.isoformat() for field, day in sorted(info.deprecated_fields.items())
        },
        "deprecated_field_details": [
            dep.to_doc() for dep in info.resolved_field_deprecations()
        ],
    }


def _iter_api_routes(app: FastAPI) -> list[APIRoute]:
    """展开应用路由；兼容惰性挂载的 _IncludedRouter。"""
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            routes.extend(r for r in original.routes if isinstance(r, APIRoute))
    return routes


def generate_contract(app: FastAPI, registry: VersionRegistry) -> dict[str, Any]:
    """生成全量契约文档（确定性输出）。"""
    endpoints: list[dict[str, Any]] = []
    for route in _iter_api_routes(app):
        if not route.include_in_schema:
            continue
        version = _version_of(route)
        if version is None:
            continue
        request_model = (
            getattr(route.body_field.field_info, "annotation", None)
            if route.body_field
            else None
        )
        endpoints.append(
            {
                "version": version,
                "method": sorted(route.methods)[0],
                "path": route.path,
                "summary": route.summary or route.name,
                "request": _schema_of(request_model),
                "response": _schema_of(route.response_model),
            }
        )
    endpoints.sort(key=lambda e: (e["version"], e["path"], e["method"]))

    return {
        "versions": [_version_block(i) for i in registry.all()],
        "endpoints": endpoints,
        "errors": {
            "envelope": ErrorEnvelope.model_json_schema(),
            "codes": sorted(c.value for c in ErrorCode),
        },
    }


def contract_for_version(contract: dict[str, Any], version: str) -> dict[str, Any]:
    """从全量契约中切出单个版本的契约片段。"""
    return {
        "version": next(v for v in contract["versions"] if v["name"] == version),
        "endpoints": [e for e in contract["endpoints"] if e["version"] == version],
        "errors": contract["errors"],
    }


def render_json(data: dict[str, Any]) -> str:
    """确定性 JSON 序列化：排序键 + 固定缩进 + 末尾换行。"""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


class SnapshotConflictError(Exception):
    """已存在的历史快照与新生成内容不一致，拒绝覆盖。"""


class BreakingContractChangeError(SnapshotConflictError):
    """新生成契约对历史快照存在破坏性变更，默认不允许写入历史版本契约。

    reports 携带各版本的差异报告，供调用方（脚本/服务端点）展示给人或程序。
    """

    def __init__(self, message: str, reports: list[ContractDiff]) -> None:
        super().__init__(message)
        self.reports = reports


# 目录级读写锁注册表：同一快照目录的所有生成/写入在此锁下串行执行，
# 不同目录互不阻塞。
_directory_locks_guard = threading.Lock()
_directory_locks: dict[str, threading.RLock] = {}


def _lock_for(directory: Path) -> threading.RLock:
    key = str(directory.resolve(strict=False))
    with _directory_locks_guard:
        lock = _directory_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _directory_locks[key] = lock
        return lock


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写文本：同目录临时文件 -> flush/fsync -> os.replace，外部永远看不到半截内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{id(content)}"
    )
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def diff_snapshot(
    app: FastAPI,
    registry: VersionRegistry,
    directory: Path,
    version: str,
) -> ContractDiff:
    """把指定版本磁盘上的历史快照与当前代码新生成结果做差异比对。

    与 write_snapshots 共用同一把目录锁，保证比对拿到的一定是完整一致的快照。
    """
    with _lock_for(directory):
        contract = generate_contract(app, registry)
        new_block = contract_for_version(contract, version)
        old_block = _load_snapshot(directory / f"contract-{version}.json")
        return diff_version_contract(old_block, new_block, version)


def write_snapshots(
    app: FastAPI,
    registry: VersionRegistry,
    directory: Path,
    *,
    allow_update: bool = False,
    allow_breaking: bool = False,
) -> list[Path]:
    """按版本写快照文件。

    门禁顺序（全部版本先校验、再统一写盘，任何一个不过都不写任何文件）：
    1. 快照已存在且内容不同：默认拒绝（SnapshotConflictError），
       需 allow_update 才允许更新；
    2. 变化中包含破坏性变更：即使 allow_update 也拒绝（BreakingContractChangeError），
       需额外显式 allow_breaking 放行。
    """
    lock = _lock_for(directory)
    with lock:
        contract = generate_contract(app, registry)
        directory.mkdir(parents=True, exist_ok=True)

        versions = [v.name for v in registry.all()]
        contents: dict[str, str] = {}
        reports: list[ContractDiff] = []
        any_difference = False

        # ---- 阶段一：生成 + 全部校验（不碰磁盘）----
        for version in versions:
            path = directory / f"contract-{version}.json"
            content = render_json(contract_for_version(contract, version))
            contents[version] = content
            if not path.exists():
                continue
            if path.read_text(encoding="utf-8") == content:
                continue
            any_difference = True
            old_block = json.loads(path.read_text(encoding="utf-8"))
            report = diff_version_contract(old_block, json.loads(content), version)
            reports.append(report)

        if any_difference and not allow_update:
            raise SnapshotConflictError(
                f"snapshots in {directory} differ from generated contract; "
                "refusing to overwrite history (use allow_update to accept compatible changes)"
            )
        breaking = [r for r in reports if not r.is_compatible]
        if breaking and not allow_breaking:
            raise BreakingContractChangeError(
                "breaking contract changes detected; refusing to rewrite historical "
                f"snapshots in {directory} (use allow_breaking to accept explicitly)",
                reports,
            )

        # ---- 阶段二：全部通过后逐个原子落盘 ----
        written: list[Path] = []
        for version in versions:
            path = directory / f"contract-{version}.json"
            _atomic_write_text(path, contents[version])
            written.append(path)
        return written
