import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pathlib import Path

from app.docs import generate_contract
from app.errors import register_exception_handlers
from app.middleware import VersionGateMiddleware
from app.routes import v1, v2
from app.schemas.common import HealthResponse
from app.snapshot import SnapshotStore
from app.versioning import build_default_registry

registry = build_default_registry()
snapshot_store = SnapshotStore(Path(__file__).resolve().parent / "contracts")

app = FastAPI(title="versioned-api")
register_exception_handlers(app)
app.add_middleware(VersionGateMiddleware, registry=registry)
app.include_router(v1.router)
app.include_router(v2.router)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/contract", include_in_schema=False)
def contract() -> dict:
    """自描述契约文档：版本生命周期、端点模型与统一错误结构。"""
    return generate_contract(app, registry)


@app.get("/contract/diff", include_in_schema=False)
def contract_diff(version: str | None = None) -> JSONResponse:
    """历史快照与当前代码生成契约的差异报告（兼容/破坏性分类）。

    - 不带参数：返回全量报告；
    - ``?version=v1``：只返回该版本的切片报告，未知版本返回 404。
    """
    report = snapshot_store.diff_against_disk(app, registry)
    if version is not None:
        slice_report = report.get("versions", {}).get(version)
        if slice_report is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"no contract report for version: {version}"},
            )
        return JSONResponse(content=slice_report)
    return JSONResponse(content=report)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
