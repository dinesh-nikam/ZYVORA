"""Synchronous HTTP client for the PHONEINTEL REST API.

Credentials: an explicit API key wins; otherwise the stored session (access +
refresh token).  A 401 on an expired access token triggers one transparent
refresh (rotated tokens are saved), then the request is retried once.
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from phoneintel_cli import __version__
from phoneintel_cli.config import CLIConfig

# Process exit codes (documented in README / --help).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_FORBIDDEN = 4
EXIT_NOT_FOUND = 5
EXIT_VALIDATION = 6
EXIT_RATE_LIMITED = 7
EXIT_CONFLICT = 8
EXIT_NETWORK = 10
EXIT_INTERRUPTED = 130

_STATUS_EXIT = {
    401: EXIT_AUTH,
    403: EXIT_FORBIDDEN,
    404: EXIT_NOT_FOUND,
    409: EXIT_CONFLICT,
    413: EXIT_VALIDATION,
    422: EXIT_VALIDATION,
    429: EXIT_RATE_LIMITED,
}


class APIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details
        self.request_id = request_id

    @property
    def exit_code(self) -> int:
        if self.status_code is None:
            return EXIT_NETWORK
        if self.status_code >= 500:
            return EXIT_NETWORK
        return _STATUS_EXIT.get(self.status_code, EXIT_ERROR)


class PhoneIntelClient:
    def __init__(
        self,
        config: CLIConfig,
        *,
        api_key: str | None = None,
        verbose: bool = False,
        log: Callable[[str], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.api_key = api_key or config.api_key
        self.verbose = verbose
        self._log = log or (lambda msg: sys.stderr.write(msg + "\n"))
        self._http = httpx.Client(
            base_url=config.api_url,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            transport=transport,
            headers={"User-Agent": f"phoneintel-cli/{__version__}", "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PhoneIntelClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ transport
    def _auth_headers(self) -> dict[str, str]:
        if self.api_key:
            return {"X-API-Key": self.api_key}
        if self.config.access_token:
            return {"Authorization": f"Bearer {self.config.access_token}"}
        return {}

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._auth_headers(), **kwargs.pop("headers", {})}
        started = time.monotonic()
        try:
            response = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise APIError(f"Request timed out after {self.config.timeout_seconds:.0f}s ({method} {path})") from exc
        except httpx.HTTPError as exc:
            raise APIError(f"Cannot reach PHONEINTEL API at {self.config.api_url}: {type(exc).__name__}") from exc
        if self.verbose:
            elapsed_ms = (time.monotonic() - started) * 1000
            req_id = response.headers.get("x-request-id")
            self._log(f"[debug] {method} {path} -> {response.status_code} ({elapsed_ms:.0f} ms, req_id={req_id})")
        return response

    def request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any) -> httpx.Response:
        response = self._send(method, path, **kwargs)
        if (
            response.status_code == 401
            and retry_auth
            and not self.api_key
            and self.config.refresh_token
            and self._refresh()
        ):
            response = self._send(method, path, **kwargs)
        if response.is_error:
            raise self._error(response)
        return response

    def _refresh(self) -> bool:
        response = self._send("POST", "/api/v1/auth/refresh", json={"refresh_token": self.config.refresh_token})
        if response.status_code != 200:
            self.config.clear_session()
            self.config.save()
            return False
        data = response.json()
        self.config.access_token = data["access_token"]
        self.config.refresh_token = data["refresh_token"]
        self.config.save()
        if self.verbose:
            self._log("[debug] access token refreshed")
        return True

    @staticmethod
    def _error(response: httpx.Response) -> APIError:
        message, code, details = f"HTTP {response.status_code}", None, None
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                message = err.get("message") or message
                code = err.get("code")
                details = err.get("details")
        except ValueError:
            message = response.reason_phrase or message
        if response.status_code == 429 and response.headers.get("retry-after"):
            message += f" (retry after {response.headers['retry-after']}s)"
        return APIError(
            message,
            status_code=response.status_code,
            code=code,
            details=details,
            request_id=response.headers.get("x-request-id"),
        )

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        return response.json() if response.content else None

    # ---------------------------------------------------------------- auth
    def login(self, email: str, password: str) -> dict[str, Any]:
        data = self.json("POST", "/api/v1/auth/login", json={"email": email, "password": password}, retry_auth=False)
        self.config.access_token = data["access_token"]
        self.config.refresh_token = data["refresh_token"]
        self.config.save()
        return data

    def register(self, email: str, password: str) -> dict[str, Any]:
        return self.json("POST", "/api/v1/auth/register", json={"email": email, "password": password}, retry_auth=False)

    def logout(self, all_sessions: bool = False) -> None:
        if self.config.access_token:
            self.request(
                "POST",
                "/api/v1/auth/logout",
                json={"refresh_token": self.config.refresh_token, "all_sessions": all_sessions},
            )
        self.config.clear_session()
        self.config.save()

    def me(self) -> dict[str, Any]:
        return self.json("GET", "/api/v1/auth/me")

    # --------------------------------------------------------------- phone
    def validate_phone(self, phone: str, region: str | None = None) -> dict[str, Any]:
        return self.json("POST", "/api/v1/phone/validate", json={"phone": phone, "region": region})

    def phone_metadata(self, phone: str, region: str | None = None) -> dict[str, Any]:
        return self.json("POST", "/api/v1/phone/metadata", json={"phone": phone, "region": region})

    # ------------------------------------------------------- investigations
    def create_investigation(
        self, phone: str, *, purpose: str, basis: str, region: str | None = None, reference: str | None = None
    ) -> dict[str, Any]:
        body = {
            "phone": phone,
            "region": region,
            "purpose": purpose,
            "authorization_basis": basis,
            "authorization_reference": reference,
            "acknowledge_limitations": True,
        }
        return self.json("POST", "/api/v1/investigations", json=body)

    def start_discovery(
        self,
        investigation_id: str,
        *,
        platforms: list[str] | None,
        profile_urls: list[str] | None,
        include_web_search: bool,
        live_checks: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "include_web_search": include_web_search,
            "live_checks": live_checks,
            "platforms": platforms or None,
            "profile_urls": profile_urls or None,
        }
        return self.json(
            "POST",
            f"/api/v1/investigations/{investigation_id}/profile-discovery",
            json=body,
            headers={"Idempotency-Key": f"cli-{uuid.uuid4().hex}"},
        )

    def get_investigation(self, investigation_id: str) -> dict[str, Any]:
        return self.json("GET", f"/api/v1/investigations/{investigation_id}")

    def cancel_investigation(self, investigation_id: str) -> dict[str, Any]:
        return self.json("POST", f"/api/v1/investigations/{investigation_id}/cancel")

    def list_investigations(self, **params: Any) -> dict[str, Any]:
        return self.json("GET", "/api/v1/investigations", params={k: v for k, v in params.items() if v is not None})

    def delete_investigation(self, investigation_id: str) -> None:
        self.request("DELETE", f"/api/v1/investigations/{investigation_id}")

    def erase_my_data(self, include_account: bool) -> dict[str, Any]:
        return self.json("DELETE", "/api/v1/me/data", params={"include_account": include_account})

    def list_profiles(self, investigation_id: str, **params: Any) -> dict[str, Any]:
        return self.json(
            "GET",
            f"/api/v1/investigations/{investigation_id}/profiles",
            params={"page_size": 200, **{k: v for k, v in params.items() if v is not None}},
        )

    def verify_url(self, url: str, phone: str | None = None) -> dict[str, Any]:
        return self.json("POST", "/api/v1/profiles/verify-url", json={"url": url, "phone": phone})

    # -------------------------------------------------------------- reports
    def get_report(self, investigation_id: str, mask_phone: bool = False) -> dict[str, Any]:
        return self.json("GET", f"/api/v1/investigations/{investigation_id}/report", params={"mask_phone": mask_phone})

    def export_report(self, investigation_id: str, fmt: str, mask_phone: bool = False) -> tuple[bytes, str]:
        response = self.request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report/export",
            params={"format": fmt, "mask_phone": mask_phone},
        )
        disposition = response.headers.get("content-disposition", "")
        filename = disposition.split("filename=")[-1].strip('"') if "filename=" in disposition else f"report.{fmt}"
        return response.content, filename

    # ------------------------------------------------------------- api keys
    def create_api_key(self, name: str, permissions: list[str] | None, expires_in_days: int) -> dict[str, Any]:
        return self.json(
            "POST",
            "/api/v1/api-keys",
            json={"name": name, "permissions": permissions or None, "expires_in_days": expires_in_days},
        )

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self.json("GET", "/api/v1/api-keys")

    def revoke_api_key(self, key_id: str) -> None:
        self.request("DELETE", f"/api/v1/api-keys/{key_id}")

    def telegram_link_code(self) -> dict[str, Any]:
        return self.json("POST", "/api/v1/integrations/telegram/link-code")
