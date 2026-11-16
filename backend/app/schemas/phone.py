from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.common import APIModel


class PhoneIn(APIModel):
    phone: str = Field(min_length=3, max_length=32, examples=["+919876543210"])
    region: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        pattern=r"^[A-Za-z]{2}$",
        description="ISO-3166 region hint for numbers given in national format",
    )


class PhoneValidationOut(APIModel):
    e164: str
    international: str
    national: str
    country_code: int
    region_code: str | None
    country_name: str | None
    number_type: str
    validation_status: str
    is_valid: bool
    is_possible: bool
    possibility_reason: str
    limitations: list[str]


class PhoneMetadataOut(PhoneValidationOut):
    rfc3966: str
    region_description: str | None
    timezones: list[str]
    carrier: str | None
    carrier_source: str
    metadata_provider: str
    checked_at: datetime
