import uvicorn
from fastapi import FastAPI, HTTPException

from app.docs import (
    CONTRACTS_DIR,
    diff_snapshot,
    generate_contract,
)
from app.diff import render_diff_text
from app.errors import register_exception_handlers
from app.middleware import VersionGateMiddleware
from app.routes import v1, v2
from app.schemas.common import HealthResponse
from app.versioning import build_default_registry

registry = build_default_registry()

app = FastAPI(title="versioned-api")
# 供运行时字段废弃检测依赖反查版本元数据。
app.state.registry = registry
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
def contract_diff() -> dict:
    """各版本磁盘历史快照 vs 当前代码新生成结果的差异报告（程序可解析）。"""
    reports = [diff_snapshot(app, registry, CONTRACTS_DIR, v.name) for v in registry.all()]
    return {
        "directory": str(CONTRACTS_DIR),
        "compatible_overall": all(r.is_compatible for r in reports),
        "reports": [r.to_dict() for r in reports],
    }


@app.get("/contract/diff/{version}", include_in_schema=False)
def contract_diff_for(version: str, format: str = "json"):
    """单版本差异报告：format=json 返回结构化结果，format=text 返回人可读文本。"""
    if not registry.is_known(version):
        raise HTTPException(status_code=404, detail=f"unknown version: {version}")
    report = diff_snapshot(app, registry, CONTRACTS_DIR, version)
    if format == "text":
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(render_diff_text(report))
    return report.to_dict()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
