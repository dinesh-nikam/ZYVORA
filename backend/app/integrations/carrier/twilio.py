"""Optional licensed carrier lookup via Twilio Lookup v2 (line type intelligence).

Used only when TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are configured.  Without
it, carrier information comes from libphonenumber's original-allocation data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.errors import OutboundBlocked
from app.integrations.http import SafeHttpClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CarrierInfo:
    carrier: str | None
    line_type: str | None
    provider: str
    error: str | None = None


class TwilioCarrierProvider:
    name = "Twilio Lookup v2 (line_type_intelligence)"

    def __init__(self, http: SafeHttpClient, account_sid: str, auth_token: str) -> None:
        self._http = http
        self._auth = (account_sid, auth_token)

    async def lookup(self, e164: str) -> CarrierInfo:
        url = f"https://lookups.twilio.com/v2/PhoneNumbers/{quote(e164)}"
        try:
            result = await self._http.fetch(url, allowed_domains={"lookups.twilio.com"},
                                            params={"Fields": "line_type_intelligence"}, auth=self._auth,
                                            target_label="carrier:twilio")
        except (OutboundBlocked, httpx.HTTPError) as exc:
            logger.warning("carrier lookup failed", extra={"error_type": type(exc).__name__})
            return CarrierInfo(None, None, self.name, error="network_error")
        if result.status_code != 200:
            return CarrierInfo(None, None, self.name, error=f"http_{result.status_code}")
        try:
            data = json.loads(result.body)
        except ValueError:
            return CarrierInfo(None, None, self.name, error="bad_response")
        lti = data.get("line_type_intelligence") or {}
        return CarrierInfo(carrier=lti.get("carrier_name"), line_type=lti.get("type"), provider=self.name)
