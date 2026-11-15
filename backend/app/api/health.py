from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import get_container
from app.api.v1.admin import check_components
from app.core.errors import AuthenticationFailed, NotFound
from app.core.metrics import REGISTRY
from app.services.container import Container

router = APIRouter(tags=["health"])


@router.get("/health/live", include_in_schema=True)
async def live() -> dict[str, str]:
    """Liveness: the process is serving requests."""
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(container: Container = Depends(get_container)) -> JSONResponse:
    """Readiness: database and Redis reachable."""
    components = await check_components(container)
    healthy = all(c.status == "ok" for c in components.values())
    return JSONResponse(status_code=200 if healthy else 503, content={
        "status": "ok" if healthy else "down",
        "components": {k: v.model_dump() for k, v in components.items()},
    })


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, container: Container = Depends(get_container)) -> Response:
    settings = container.settings
    if not settings.metrics_enabled:
        raise NotFound("Not found")
    if settings.metrics_token is not None:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), settings.metrics_token.get_secret_value().encode()):
            raise AuthenticationFailed("Metrics token required")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
