"""生成/校验各版本契约快照。

用法：
    uv run python scripts/generate_contracts.py
        # 写快照：内容变化默认拒绝；兼容变化需 --allow-update；
        # 破坏性变更还需显式 --allow-breaking
    uv run python scripts/generate_contracts.py --check
        # 只校验，不写盘；快照与代码不一致时退出码非 0（CI 用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.diff import diff_version_contract, render_diff_text
from app.docs import (
    CONTRACTS_DIR,
    BreakingContractChangeError,
    SnapshotConflictError,
    contract_for_version,
    generate_contract,
    render_json,
    write_snapshots,
)
from main import app, registry


def main() -> int:
    check_only = "--check" in sys.argv
    allow_update = "--allow-update" in sys.argv
    allow_breaking = "--allow-breaking" in sys.argv
    contract = generate_contract(app, registry)
    ok = True

    if check_only:
        for version in [v.name for v in registry.all()]:
            path = CONTRACTS_DIR / f"contract-{version}.json"
            content = render_json(contract_for_version(contract, version))
            if not path.exists():
                print(f"MISSING  {path}")
                ok = False
            elif path.read_text(encoding="utf-8") != content:
                print(f"STALE    {path}")
                old = json.loads(path.read_text(encoding="utf-8"))
                print(render_diff_text(diff_version_contract(old, json.loads(content), version)))
                ok = False
            else:
                print(f"OK       {path}")
        return 0 if ok else 1

    try:
        written = write_snapshots(
            app,
            registry,
            CONTRACTS_DIR,
            allow_update=allow_update,
            allow_breaking=allow_breaking,
        )
    except BreakingContractChangeError as exc:
        print("BLOCKED  检测到破坏性契约变更，默认拒绝写入历史快照：", file=sys.stderr)
        for report in exc.reports:
            print(render_diff_text(report), file=sys.stderr)
        print("如确认放行，显式追加 --allow-update --allow-breaking。", file=sys.stderr)
        return 1
    except SnapshotConflictError as exc:
        print(f"CONFLICT {exc}", file=sys.stderr)
        print("若为有意的兼容演进，显式追加 --allow-update。", file=sys.stderr)
        return 1
    for path in written:
        print(f"WROTE    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
