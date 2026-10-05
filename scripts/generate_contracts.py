"""生成/校验各版本契约快照。

用法：
    uv run python scripts/generate_contracts.py           # 写快照（历史不一致则拒绝）
    uv run python scripts/generate_contracts.py --check   # 只校验，不写盘
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.docs import (
    SnapshotConflictError,
    contract_for_version,
    generate_contract,
    render_json,
)
from main import app, registry

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


def main() -> int:
    check_only = "--check" in sys.argv
    contract = generate_contract(app, registry)
    ok = True
    for version in [v.name for v in registry.all()]:
        path = CONTRACTS_DIR / f"contract-{version}.json"
        content = render_json(contract_for_version(contract, version))
        if check_only:
            if not path.exists():
                print(f"MISSING  {path}")
                ok = False
            elif path.read_text(encoding="utf-8") != content:
                print(f"STALE    {path}")
                ok = False
            else:
                print(f"OK       {path}")
        else:
            if path.exists() and path.read_text(encoding="utf-8") != content:
                print(f"CONFLICT {path} (历史快照与新生成内容不一致，拒绝覆盖)")
                ok = False
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"WROTE    {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
