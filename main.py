import uvicorn
from fastapi import FastAPI

from app.docs import generate_contract
from app.errors import register_exception_handlers
from app.middleware import VersionGateMiddleware
from app.routes import v1, v2
from app.schemas.common import HealthResponse
from app.versioning import build_default_registry

registry = build_default_registry()

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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
