"""Async PHONEINTEL API client used by the bot.

The bot authenticates with its *service* API key and acts on behalf of the
Telegram user by sending ``X-Telegram-User-Id``.  The backend resolves that id
to the linked PHONEINTEL account and applies that account's permissions and
data isolation; the bot never sees other users' records.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from phoneintel_bot.config import BotSettings

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REFERENCE = re.compile(r"^PI-\d{8}-[A-Z0-9]{6}$", re.IGNORECASE)


class BotAPIError(Exception):
    def __init__(
        self, message: str, *, code: str | None = None, status_code: int | None = None, details: Any = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details


class BackendClient:
    def __init__(self, settings: BotSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=settings.request_timeout_seconds,
            transport=transport,
            headers={
                "User-Agent": "phoneintel-telegram-bot/1.0",
                "Accept": "application/json",
                "X-API-Key": settings.service_api_key.get_secret_value(),
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, telegram_user_id: int | None = None, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if telegram_user_id is not None:
            headers["X-Telegram-User-Id"] = str(int(telegram_user_id))
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BotAPIError("The PHONEINTEL service timed out", code="timeout") from exc
        except httpx.HTTPError as exc:
            raise BotAPIError("The PHONEINTEL service is unreachable", code="backend_unavailable") from exc
        if response.is_error:
            code, message, details = "error", f"HTTP {response.status_code}", None
            try:
                err = response.json().get("error") or {}
                code = err.get("code") or code
                message = err.get("message") or message
                details = err.get("details")
            except (ValueError, AttributeError):
                message = response.reason_phrase or message
            raise BotAPIError(message, code=code, status_code=response.status_code, details=details)
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        return response.json() if response.content else None

    # ---------------------------------------------------------- linking
    async def link_account(self, telegram_user_id: int, code: str) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/v1/integrations/telegram/link",
            json={"telegram_user_id": telegram_user_id, "code": code.strip().upper()},
        )

    async def unlink_account(self, telegram_user_id: int) -> dict[str, Any]:
        return await self._json(
            "POST", "/api/v1/integrations/telegram/unlink", json={"telegram_user_id": telegram_user_id}
        )

    # ------------------------------------------------------------ phone
    async def validate_phone(self, uid: int, phone: str, region: str | None = None) -> dict[str, Any]:
        return await self._json(
            "POST", "/api/v1/phone/validate", telegram_user_id=uid, json={"phone": phone, "region": region}
        )

    async def phone_metadata(self, uid: int, phone: str, region: str | None = None) -> dict[str, Any]:
        return await self._json(
            "POST", "/api/v1/phone/metadata", telegram_user_id=uid, json={"phone": phone, "region": region}
        )

    # --------------------------------------------------- investigations
    async def create_investigation(self, uid: int, *, phone: str, purpose: str, basis: str) -> dict[str, Any]:
        return await self._json(
            "POST",
            "/api/v1/investigations",
            telegram_user_id=uid,
            json={
                "phone": phone,
                "purpose": purpose,
                "authorization_basis": basis,
                "authorization_reference": "telegram-bot",
                "acknowledge_limitations": True,
            },
        )

    async def start_discovery(
        self, uid: int, investigation_id: str, *, live_checks: bool, idempotency_key: str
    ) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"/api/v1/investigations/{investigation_id}/profile-discovery",
            telegram_user_id=uid,
            json={"include_web_search": True, "live_checks": live_checks},
            headers={"Idempotency-Key": idempotency_key},
        )

    async def get_investigation(self, uid: int, investigation_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/v1/investigations/{investigation_id}", telegram_user_id=uid)

    async def cancel_investigation(self, uid: int, investigation_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/v1/investigations/{investigation_id}/cancel", telegram_user_id=uid)

    async def list_profiles(self, uid: int, investigation_id: str) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"/api/v1/investigations/{investigation_id}/profiles",
            telegram_user_id=uid,
            params={"page_size": 200},
        )

    async def list_investigations(
        self,
        uid: int,
        *,
        page_size: int = 10,
        reference: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page": 1, "page_size": page_size}
        if reference:
            params["reference"] = reference
        return await self._json("GET", "/api/v1/investigations", telegram_user_id=uid, params=params)

    async def resolve_investigation(self, uid: int, token: str | None) -> str | None:
        """Accept a UUID, a PI-... reference, or nothing (= most recent)."""
        if token and _UUID.match(token):
            return token
        if token and not _REFERENCE.match(token):
            raise BotAPIError(
                "Give an investigation id or a reference like PI-20261114-ABC123", code="invalid_reference"
            )
        data = await self.list_investigations(uid, page_size=1, reference=token.upper() if token else None)
        items = data.get("items") or []
        return str(items[0]["id"]) if items else None

    async def delete_investigation(self, uid: int, investigation_id: str) -> None:
        await self._request("DELETE", f"/api/v1/investigations/{investigation_id}", telegram_user_id=uid)

    async def erase_all(self, uid: int) -> dict[str, Any]:
        return await self._json("DELETE", "/api/v1/me/data", telegram_user_id=uid, params={"include_account": False})

    # ---------------------------------------------------------- reports
    async def get_report(self, uid: int, investigation_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/v1/investigations/{investigation_id}/report", telegram_user_id=uid)

    async def export_report(self, uid: int, investigation_id: str, fmt: str) -> tuple[bytes, str]:
        response = await self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report/export",
            telegram_user_id=uid,
            params={"format": fmt},
        )
        disposition = response.headers.get("content-disposition", "")
        name = disposition.split("filename=")[-1].strip('"') if "filename=" in disposition else f"report.{fmt}"
        return response.content, name
