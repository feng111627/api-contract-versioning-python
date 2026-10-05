"""应用组装入口：版本登记、中间件、路由、异常归一化、文档端点。"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api import v1, v2
from app.docs import build_docs_routes, write_contract_file
from app.errors import (
    DEFAULT_STATUS,
    APIError,
    ErrorCode,
    error_body,
    normalize_field_errors,
)
from app.versioning import (
    DeprecationHeaderMiddleware,
    VersionGuardMiddleware,
    VersionInfo,
    VersionRegistry,
    VersionStatus,
)

logger = logging.getLogger("api")


def build_registry() -> VersionRegistry:
    """登记全部版本及其生命周期状态（唯一事实源）。"""
    return VersionRegistry(
        [
            VersionInfo(
                name="v0",
                status=VersionStatus.RETIRED,
                introduced="2023-01-01",
                deprecated_since="2024-06-01",
                sunset=date(2025, 1, 1),
                successor="v1",
                notes="最早版本，已停止服务，访问一律返回 410 VERSION_RETIRED。",
            ),
            VersionInfo(
                name="v1",
                status=VersionStatus.DEPRECATED,
                introduced="2024-01-01",
                deprecated_since="2026-10-06",
                sunset=date(2027, 4, 1),
                successor="v2",
                notes="用户模型使用单字段 name；请迁移到 v2 的 first_name/last_name。",
            ),
            VersionInfo(
                name="v2",
                status=VersionStatus.ACTIVE,
                introduced="2026-10-06",
                notes="当前推荐版本；email 字段已标记为字段级废弃。",
            ),
        ]
    )


def register_exception_handlers(app: FastAPI) -> None:
    """安装归一化异常处理器：所有错误都落成统一信封。"""

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 框架层参数校验失败（类型/必填/正则等）→ 422 VALIDATION_ERROR
        return JSONResponse(
            error_body(
                ErrorCode.VALIDATION_ERROR,
                "请求参数校验失败。",
                normalize_field_errors(exc.errors()),
            ),
            status_code=DEFAULT_STATUS[ErrorCode.VALIDATION_ERROR],
        )

    @app.exception_handler(APIError)
    async def on_api_error(request: Request, exc: APIError) -> JSONResponse:
        # 业务异常：错误码自带稳定 HTTP 状态
        return JSONResponse(exc.to_body(), status_code=exc.status_code)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 已知版本下的路径 404（如 /v2/unknown）：归一化为统一信封，
        # 但错误码与 VERSION_UNKNOWN 不同，调用方可区分“版本不对”与“路径不对”。
        if exc.status_code == 404:
            return JSONResponse(
                error_body(ErrorCode.RESOURCE_NOT_FOUND, "请求的路径或资源不存在。"),
                status_code=404,
            )
        if exc.status_code == 405:
            return JSONResponse(
                error_body(ErrorCode.BAD_REQUEST, "请求方法不被允许。"),
                status_code=405,
            )
        return JSONResponse(
            error_body(ErrorCode.BAD_REQUEST, "请求无法被处理。"),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：只记录服务端日志，对外返回固定文案，绝不泄露类型/堆栈
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            error_body(
                ErrorCode.INTERNAL_ERROR,
                "服务内部错误，请稍后重试。",
            ),
            status_code=DEFAULT_STATUS[ErrorCode.INTERNAL_ERROR],
        )


def create_app(*, write_snapshot: str | None = None) -> FastAPI:
    """构造并返回完整的 FastAPI 应用。"""
    app = FastAPI(
        title="API Contract Versioning",
        version="2.0.0",
        docs_url=None,
        redoc_url=None,
    )
    versions = build_registry()

    # 中间件执行顺序：后添加的先执行。
    # Deprecation 头在外层 → 即使守卫拒绝（410/版本未知）也能附带版本上下文头；
    # Guard 在内层负责拦截已下线/未知版本。
    app.add_middleware(DeprecationHeaderMiddleware, versions=versions)
    app.add_middleware(VersionGuardMiddleware, versions=versions)

    register_exception_handlers(app)

    v1.install(app)
    v2.install(app)
    build_docs_routes(app, versions)

    @app.get("/", include_in_schema=False, tags=["meta"])
    def index() -> dict[str, object]:
        return {
            "service": "api-contract-versioning",
            "recommended_version": "v2",
            "contract_url": "/docs/contract",
            "versions": [info.to_dict() for info in versions.all()],
        }

    @app.get("/health", include_in_schema=False, tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if write_snapshot:
        write_contract_file(app, versions, write_snapshot)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
