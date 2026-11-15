"""Domain errors and the consistent API error envelope.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "details": [...], "request_id": "..."}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: list[Any] | None = None,
                 status_code: int | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.details = details or []
        self.headers = headers or {}


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class AuthenticationFailed(AppError):
    status_code = 401
    code = "authentication_failed"

    def __init__(self, message: str = "Authentication required", **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class OutboundBlocked(AppError):
    """An outbound request was refused by SSRF / allowlist policy."""

    status_code = 422
    code = "outbound_blocked"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"


def error_body(code: str, message: str, request_id: str | None, details: list[Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or [], "request_id": request_id}}
