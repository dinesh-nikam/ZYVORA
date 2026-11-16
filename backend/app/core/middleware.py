"""Pure-ASGI middleware: request ids, security headers, body-size limit, metrics."""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_body
from app.core.logging import request_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"geolocation=(), camera=(), microphone=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
_DOCS_CSP = (
    b"content-security-policy",
    b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"img-src 'self' data: https://fastapi.tiangolo.com; "
    b"frame-ancestors 'none'",
)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int, hsts: bool) -> None:
        self.app = app
        self.max_body = max_body_bytes
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        incoming = headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status_holder = {"status": 500}
        path: str = scope.get("path", "")
        is_docs = path.startswith(("/docs", "/redoc", "/openapi.json"))

        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body:
            await self._reject_too_large(send, request_id)
            request_id_var.reset(token)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge
            return message

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                raw: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                existing = {k.lower() for k, _ in raw}
                raw.append((b"x-request-id", request_id.encode()))
                for name, value in [*SECURITY_HEADERS, _DOCS_CSP if is_docs else _API_CSP]:
                    if name not in existing:
                        raw.append((name, value))
                if b"cache-control" not in existing:
                    raw.append((b"cache-control", b"no-store"))
                if self.hsts:
                    raw.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except _BodyTooLarge:
            await self._reject_too_large(send, request_id)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "GET")
            HTTP_REQUESTS.labels(method=method, route=template, status=str(status_holder["status"])).inc()
            HTTP_LATENCY.labels(method=method, route=template).observe(time.perf_counter() - started)
            request_id_var.reset(token)

    @staticmethod
    async def _reject_too_large(send: Send, request_id: str) -> None:
        body = json.dumps(error_body("payload_too_large", "Request body too large", request_id)).encode()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"x-request-id", request_id.encode()),
            *SECURITY_HEADERS,
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class HttpsEnforcementMiddleware:
    """Redirects/rejects plain-HTTP requests (scheme as reported by the trusted proxy).

    ``/health/*`` and ``/metrics`` stay reachable over HTTP for in-cluster probes.
    """

    _EXEMPT = ("/health/", "/metrics")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] not in ("http", "websocket")
            or scope.get("scheme") in ("https", "wss")
            or scope.get("path", "").startswith(self._EXEMPT)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), "")
        if scope.get("method") in ("GET", "HEAD") and host:
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{host}{scope.get('path', '/')}" + (f"?{query}" if query else "")
            await send(
                {
                    "type": "http.response.start",
                    "status": 308,
                    "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        body = json.dumps(error_body("https_required", "HTTPS is required", None)).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


def client_ip_from_scope(scope: dict[str, Any]) -> str | None:
    client = scope.get("client")
    return client[0] if client else None
