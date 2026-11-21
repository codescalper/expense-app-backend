"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import inline_job_delivery
from app.api.internal import router as internal_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware
from app.db.session import close_database, get_database

log = get_logger(__name__)
MAX_REQUEST_BYTES = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log.info(
        "startup",
        environment=settings.environment,
        serverless=settings.serverless,
        email_delivery=settings.email_delivery,
        storage_backend=settings.storage_backend,
    )
    for warning in settings.deployment_warnings():
        log.warning("config_warning", detail=warning)
    yield
    await close_database()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.environment)
    docs = settings.enable_docs and settings.environment != "production"
    app = FastAPI(
        title=settings.app_name,
        version="0.2.0",
        description="Family budget & expense management API.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    register_exception_handlers(app)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=False,  # bearer tokens, no cookies
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
            max_age=600,
        )
    app.add_middleware(
        RequestContextMiddleware,
        hsts=settings.is_production_like,
        max_body_bytes=MAX_REQUEST_BYTES,
    )

    @app.get("/health", tags=["health"], include_in_schema=False)
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], include_in_schema=False)
    async def ready() -> JSONResponse:
        """Readiness: the database is reachable."""
        try:
            async with get_database().engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - report any failure as not ready
            log.warning("readiness_failed", error_type=type(exc).__name__)
            return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "up"})

    app.include_router(
        api_router,
        prefix=settings.api_v1_prefix,
        dependencies=[Depends(inline_job_delivery)],
    )
    app.include_router(internal_router)
    return app


app = create_app()
