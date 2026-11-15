"""HTTP Client for PHONEINTEL REST API."""

from __future__ import annotations

from typing import Any

import httpx

from phoneintel_cli.config import CLIConfig


class APIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class PhoneIntelClient:
    def __init__(self, config: CLIConfig | None = None, timeout: float = 30.0) -> None:
        self.config = config or CLIConfig.load()
        self.base_url = self.config.api_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": "phoneintel-cli/1.0.0",
            "Accept": "application/json",
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        elif self.config.api_key:
            headers["X-API-Key"] = self.config.api_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        req_headers = self._headers()
        if headers:
            req_headers.update(headers)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(method, url, json=json, params=params, headers=req_headers)
        except httpx.ConnectError as exc:
            raise APIError(f"Cannot connect to PHONEINTEL API at {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise APIError(f"Request to {url} timed out after {self.timeout}s") from exc
        except httpx.RequestError as exc:
            raise APIError(f"Network error: {exc}") from exc

        if response.is_error:
            msg = f"HTTP {response.status_code}"
            details = None
            try:
                err_data = response.json()
                if "error" in err_data:
                    msg = err_data["error"].get("message") or msg
                    details = err_data["error"].get("details")
                elif "detail" in err_data:
                    msg = str(err_data["detail"])
            except Exception:
                msg = response.text or msg
            raise APIError(msg, status_code=response.status_code, details=details)

        return response

    # ---------------- Auth endpoints ----------------
    def register(self, email: str, password: str) -> dict[str, Any]:
        resp = self._request("POST", "/api/v1/auth/register", json={"email": email, "password": password})
        return resp.json()

    def login(self, email: str, password: str) -> dict[str, Any]:
        resp = self._request("POST", "/api/v1/auth/login", json={"email": email, "password": password})
        data = resp.json()
        if "access_token" in data:
            self.config.access_token = data["access_token"]
            self.config.save()
        return data

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/me").json()

    # ---------------- Phone Intelligence ----------------
    def validate_phone(self, phone: str, region: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/v1/phone/validate", json={"phone": phone, "region": region}).json()

    def phone_metadata(self, phone: str, region: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/api/v1/phone/metadata", json={"phone": phone, "region": region}).json()

    # ---------------- Investigations & Discovery ----------------
    def create_investigation(
        self,
        phone: str,
        *,
        region: str | None = None,
        purpose: str | None = None,
        basis: str | None = None,
        reference: str | None = None,
    ) -> dict[str, Any]:
        purpose_str = purpose or self.config.default_purpose
        basis_str = basis or self.config.default_basis
        body = {
            "phone": phone,
            "region": region,
            "purpose": purpose_str,
            "authorization_basis": basis_str,
            "authorization_reference": reference,
            "acknowledge_limitations": True,
        }
        return self._request("POST", "/api/v1/investigations", json=body).json()

    def start_discovery(
        self,
        investigation_id: str,
        *,
        platforms: list[str] | None = None,
        profile_urls: list[str] | None = None,
        include_web_search: bool = True,
        live_checks: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "include_web_search": include_web_search,
            "live_checks": live_checks,
        }
        if platforms:
            body["platforms"] = platforms
        if profile_urls:
            body["profile_urls"] = profile_urls
        return self._request("POST", f"/api/v1/investigations/{investigation_id}/profile-discovery", json=body).json()

    def get_investigation(self, investigation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/investigations/{investigation_id}").json()

    def list_investigations(self, page: int = 1, page_size: int = 20, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        return self._request("GET", "/api/v1/investigations", params=params).json()

    def delete_investigation(self, investigation_id: str) -> None:
        self._request("DELETE", f"/api/v1/investigations/{investigation_id}")

    def list_profiles(self, investigation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/investigations/{investigation_id}/profiles").json()

    def verify_url(self, url: str, phone: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url}
        if phone:
            body["phone"] = phone
        return self._request("POST", "/api/v1/profiles/verify-url", json=body).json()

    # ---------------- Reports ----------------
    def get_report(self, investigation_id: str, mask_phone: bool = False) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report",
            params={"mask_phone": mask_phone},
        ).json()

    def export_report(self, investigation_id: str, fmt: str = "pdf", mask_phone: bool = False) -> bytes:
        resp = self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report/export",
            params={"format": fmt, "mask_phone": mask_phone},
        )
        return resp.content

    # ---------------- Telegram Link ----------------
    def create_telegram_link_code(self) -> dict[str, Any]:
        return self._request("POST", "/api/v1/integrations/telegram/link-code").json()
