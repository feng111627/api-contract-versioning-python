"""生成/校验各版本契约快照。

用法：
    uv run python scripts/generate_contracts.py                  # 写快照（默认拒绝任何历史变更）
    uv run python scripts/generate_contracts.py --allow-update   # 仅含兼容变化时放行
    uv run python scripts/generate_contracts.py --allow-breaking # 显式接受破坏性变更（含兼容）
    uv run python scripts/generate_contracts.py --check          # 只校验，不写盘（CI 用）
    uv run python scripts/generate_contracts.py --diff           # 只打印差异报告，不写盘
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docs import contract_for_version, generate_contract, render_json
from app.snapshot import BreakingChangeError, SnapshotStore
from main import app, registry

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


def _print_report(report: dict) -> None:
    if not report.get("has_changes"):
        print("无差异：磁盘快照与当前代码生成的契约一致")
        return
    versions = report.get("versions", {})
    for name in sorted(versions):
        version_report = versions[name]
        if not version_report.get("has_changes"):
            print(f"[{name}] 无变化")
            continue
        print(
            f"[{name}] 破坏性 {version_report['breaking_change_count']} 条 / "
            f"兼容 {version_report['compatible_change_count']} 条"
        )
        for change in version_report["changes"]:
            mark = "!" if change["category"] == "breaking" else "+"
            print(f"  {mark} [{change['kind']}] {change['path']}")
            print(f"      {change['message']}")


def main() -> int:
    check_only = "--check" in sys.argv
    diff_only = "--diff" in sys.argv
    allow_update = "--allow-update" in sys.argv
    allow_breaking = "--allow-breaking" in sys.argv

    store = SnapshotStore(CONTRACTS_DIR)

    if diff_only:
        _print_report(store.diff_against_disk(app, registry))
        return 0

    if check_only:
        contract = generate_contract(app, registry)
        ok = True
        for version in [v.name for v in registry.all()]:
            path = CONTRACTS_DIR / f"contract-{version}.json"
            content = render_json(contract_for_version(contract, version))
            if not path.exists():
                print(f"MISSING  {path}")
                ok = False
            elif path.read_text(encoding="utf-8") != content:
                print(f"STALE    {path}（运行 --diff 查看兼容/破坏性分类）")
                ok = False
            else:
                print(f"OK       {path}")
        return 0 if ok else 1

    try:
        written = store.write_all(
            app,
            registry,
            allow_update=allow_update,
            allow_breaking=allow_breaking,
        )
    except BreakingChangeError as exc:
        print("拒绝写入：历史契约存在破坏性变更（如确认要放行，请加 --allow-breaking）")
        _print_report({"versions": {"<blocked>": exc.report}})
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 顶层统一收口
        print(f"CONFLICT {exc}（仅兼容变化可用 --allow-update 放行）")
        return 1
    for path in written:
        print(f"WROTE    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
