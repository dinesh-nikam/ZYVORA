"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import AppError, error_body
from app.core.logging import configure_logging
from app.core.middleware import HttpsEnforcementMiddleware, RequestContextMiddleware
from app.services.container import Container, build_container

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
PHONEINTEL - phone-number intelligence and **public** social-profile link discovery.

* Every investigation requires a stated purpose and authorization basis.
* Discovery uses only authorised public search APIs, official platform APIs, robots-permitted public pages and
  investigator-supplied links. No platform protections are bypassed and no number-to-account enumeration is done.
* Each result carries its evidence, a verification status and a transparent confidence score.

Authentication: `Authorization: Bearer <JWT access token>` or `X-API-Key: <key>`.
"""


def _request_id(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await build_container(settings)
        if settings.otel_enabled:
            from app.core.telemetry import setup_tracing  # noqa: PLC0415

            setup_tracing(app, app.state.container.engine, settings.otel_service_name)
        logger.info("PHONEINTEL API started", extra={"env": settings.app_env.value})
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    docs = settings.enable_docs
    app = FastAPI(
        title="PHONEINTEL API",
        version="1.0.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    if container is not None:
        app.state.container = container

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                            content=error_body(exc.code, exc.message, _request_id(request), exc.details))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details: list[dict[str, Any]] = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return JSONResponse(status_code=422, content=error_body("validation_error", "Request validation failed",
                                                                _request_id(request), details))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed", 400: "bad_request"}.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return JSONResponse(status_code=exc.status_code, content=error_body(code, message, _request_id(request)),
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(status_code=500, content=error_body("internal_error", "Internal server error",
                                                                _request_id(request)))

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    # Middleware order: last added runs first.
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After", "Idempotent-Replayed"],
            max_age=600,
        )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_request_body_bytes,
                       hsts=settings.force_https)
    if settings.force_https:
        app.add_middleware(HttpsEnforcementMiddleware)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory app.main:app_factory``."""
    return create_app()
