"""Async backend API client for Telegram Bot with delegation support."""

from __future__ import annotations

from typing import Any

import httpx

from phoneintel_bot.config import BotSettings


class BotAPIError(Exception):
    def __init__(self, message: str, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class BackendClient:
    def __init__(self, settings: BotSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.base_url = settings.api_url.rstrip("/")
        self._external_client = client

    def _headers(self, telegram_user_id: int | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "phoneintel-bot/1.0.0",
            "Accept": "application/json",
            "X-API-Key": self.settings.service_api_key.get_secret_value(),
        }
        if telegram_user_id is not None:
            headers["X-Telegram-User-Id"] = str(telegram_user_id)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        telegram_user_id: int | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        headers = self._headers(telegram_user_id)

        try:
            if self._external_client is not None:
                resp = await self._external_client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=headers,
                    timeout=self.settings.request_timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as ac:
                    resp = await ac.request(method, url, json=json, params=params, headers=headers)
        except httpx.ConnectError as exc:
            raise BotAPIError(f"Backend service unavailable: {exc}", code="backend_unavailable") from exc
        except httpx.TimeoutException as exc:
            raise BotAPIError("Request to backend timed out", code="timeout") from exc
        except httpx.RequestError as exc:
            raise BotAPIError(f"Communication error: {exc}", code="network_error") from exc

        if resp.is_error:
            code = "error"
            msg = f"HTTP {resp.status_code}"
            try:
                data = resp.json()
                if "error" in data:
                    code = data["error"].get("code", code)
                    msg = data["error"].get("message", msg)
                elif "detail" in data:
                    msg = str(data["detail"])
            except Exception:
                msg = resp.text or msg
            raise BotAPIError(msg, code=code, status_code=resp.status_code)

        return resp

    # ---------------- Linking ----------------
    async def link_account(self, telegram_user_id: int, code: str) -> dict[str, Any]:
        """Service account links Telegram user ID with one-time code."""
        resp = await self._request(
            "POST",
            "/api/v1/integrations/telegram/link",
            json={"telegram_user_id": telegram_user_id, "code": code.strip().upper()},
        )
        return resp.json()

    async def unlink_account(self, telegram_user_id: int) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/v1/integrations/telegram/unlink",
            json={"telegram_user_id": telegram_user_id},
        )
        return resp.json()

    # ---------------- Phone Endpoints ----------------
    async def validate_phone(self, telegram_user_id: int, phone: str, region: str | None = None) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/v1/phone/validate",
            telegram_user_id=telegram_user_id,
            json={"phone": phone, "region": region},
        )
        return resp.json()

    async def phone_metadata(self, telegram_user_id: int, phone: str, region: str | None = None) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/api/v1/phone/metadata",
            telegram_user_id=telegram_user_id,
            json={"phone": phone, "region": region},
        )
        return resp.json()

    # ---------------- Investigations & Profiles ----------------
    async def create_investigation(
        self,
        telegram_user_id: int,
        phone: str,
        purpose: str = "Telegram bot investigation",
        basis: str = "fraud_prevention",
        region: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "phone": phone,
            "region": region,
            "purpose": purpose,
            "authorization_basis": basis,
            "acknowledge_limitations": True,
        }
        resp = await self._request(
            "POST",
            "/api/v1/investigations",
            telegram_user_id=telegram_user_id,
            json=body,
        )
        return resp.json()

    async def start_discovery(
        self,
        telegram_user_id: int,
        investigation_id: str,
        profile_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "include_web_search": True,
            "live_checks": True,
        }
        if profile_urls:
            body["profile_urls"] = profile_urls
        resp = await self._request(
            "POST",
            f"/api/v1/investigations/{investigation_id}/profile-discovery",
            telegram_user_id=telegram_user_id,
            json=body,
        )
        return resp.json()

    async def get_investigation(self, telegram_user_id: int, investigation_id: str) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}",
            telegram_user_id=telegram_user_id,
        )
        return resp.json()

    async def list_profiles(self, telegram_user_id: int, investigation_id: str) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/profiles",
            telegram_user_id=telegram_user_id,
        )
        return resp.json()

    async def list_investigations(self, telegram_user_id: int, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            "/api/v1/investigations",
            telegram_user_id=telegram_user_id,
            params={"page": page, "page_size": page_size},
        )
        return resp.json()

    async def delete_investigation(self, telegram_user_id: int, investigation_id: str) -> None:
        await self._request(
            "DELETE",
            f"/api/v1/investigations/{investigation_id}",
            telegram_user_id=telegram_user_id,
        )

    async def get_report(
        self, telegram_user_id: int, investigation_id: str, mask_phone: bool = False
    ) -> dict[str, Any]:
        resp = await self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report",
            telegram_user_id=telegram_user_id,
            params={"mask_phone": mask_phone},
        )
        return resp.json()

    async def export_report(
        self, telegram_user_id: int, investigation_id: str, fmt: str = "pdf", mask_phone: bool = False
    ) -> bytes:
        resp = await self._request(
            "GET",
            f"/api/v1/investigations/{investigation_id}/report/export",
            telegram_user_id=telegram_user_id,
            params={"format": fmt, "mask_phone": mask_phone},
        )
        return resp.content
