"""Phone-number intelligence built on libphonenumber metadata.

Important: a number being *valid* only means it falls inside an allocated
numbering range.  It does not establish ownership, current activity or the
identity of any subscriber.  Carrier data from libphonenumber reflects the
*original* range allocation; number portability can change the actual carrier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType, carrier, geocoder, timezone

from app.core.errors import ValidationFailed
from app.models.enums import ValidationStatus

METADATA_PROVIDER = f"libphonenumber offline metadata (phonenumbers {phonenumbers.__version__})"

_ALLOWED_INPUT = re.compile(r"^[0-9+()\-.\s/]{3,32}$")

_TYPE_NAMES = {
    PhoneNumberType.FIXED_LINE: "FIXED_LINE",
    PhoneNumberType.MOBILE: "MOBILE",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "FIXED_LINE_OR_MOBILE",
    PhoneNumberType.TOLL_FREE: "TOLL_FREE",
    PhoneNumberType.PREMIUM_RATE: "PREMIUM_RATE",
    PhoneNumberType.SHARED_COST: "SHARED_COST",
    PhoneNumberType.VOIP: "VOIP",
    PhoneNumberType.PERSONAL_NUMBER: "PERSONAL_NUMBER",
    PhoneNumberType.PAGER: "PAGER",
    PhoneNumberType.UAN: "UAN",
    PhoneNumberType.VOICEMAIL: "VOICEMAIL",
    PhoneNumberType.UNKNOWN: "UNKNOWN",
}

_POSSIBILITY = {
    phonenumbers.ValidationResult.IS_POSSIBLE: "IS_POSSIBLE",
    phonenumbers.ValidationResult.IS_POSSIBLE_LOCAL_ONLY: "IS_POSSIBLE_LOCAL_ONLY",
    phonenumbers.ValidationResult.INVALID_COUNTRY_CODE: "INVALID_COUNTRY_CODE",
    phonenumbers.ValidationResult.TOO_SHORT: "TOO_SHORT",
    phonenumbers.ValidationResult.INVALID_LENGTH: "INVALID_LENGTH",
    phonenumbers.ValidationResult.TOO_LONG: "TOO_LONG",
}

LIMITATIONS = [
    "Number validity only confirms the number is inside an allocated numbering range; it does not establish "
    "ownership, current activity or subscriber identity.",
    "Carrier information (when shown) reflects the original range allocation; number portability may mean "
    "a different current carrier.",
    "Region descriptions are derived from numbering-plan metadata, not from the device's location.",
]


@dataclass(slots=True)
class PhoneIntel:
    raw_input: str
    e164: str
    international: str
    national: str
    rfc3966: str
    country_code: int
    region_code: str | None
    country_name: str | None
    region_description: str | None
    number_type: str
    validation_status: ValidationStatus
    is_valid: bool
    is_possible: bool
    possibility_reason: str
    national_significant_number: str
    timezones: list[str]
    carrier: str | None
    metadata_provider: str = METADATA_PROVIDER
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))

    @property
    def number(self) -> phonenumbers.PhoneNumber:
        return phonenumbers.parse(self.e164, None)

    def details(self) -> dict[str, object]:
        return {
            "international": self.international,
            "national": self.national,
            "rfc3966": self.rfc3966,
            "region_description": self.region_description,
            "timezones": self.timezones,
            "is_possible": self.is_possible,
            "possibility_reason": self.possibility_reason,
        }


class PhoneIntelService:
    def __init__(self, default_region: str | None = None) -> None:
        self._default_region = default_region

    def sanitize(self, raw: str) -> str:
        if not isinstance(raw, str):
            raise ValidationFailed("Phone number must be a string", code="invalid_phone_input")
        value = raw.strip()
        if not _ALLOWED_INPUT.match(value):
            raise ValidationFailed(
                "Phone number may only contain digits, spaces and + ( ) - . / (3-32 characters)",
                code="invalid_phone_input",
            )
        if value.count("+") > 1 or ("+" in value and not value.startswith("+")):
            raise ValidationFailed("'+' is only allowed as the first character", code="invalid_phone_input")
        return value

    def parse(self, raw: str, region: str | None = None) -> phonenumbers.PhoneNumber:
        value = self.sanitize(raw)
        hint = (region or self._default_region or "").upper() or None
        if hint is not None and hint not in phonenumbers.SUPPORTED_REGIONS:
            raise ValidationFailed("Unknown region code", code="invalid_region")
        if not value.startswith("+") and hint is None:
            raise ValidationFailed(
                "Number must be in international format (+<country code>...) or a region hint must be given",
                code="region_required",
            )
        try:
            return phonenumbers.parse(value, hint)
        except NumberParseException as exc:
            raise ValidationFailed(f"Not a phone number: {exc._msg}", code="invalid_phone_number") from exc

    def analyze(self, raw: str, region: str | None = None) -> PhoneIntel:
        number = self.parse(raw, region)
        is_valid = phonenumbers.is_valid_number(number)
        possibility = phonenumbers.is_possible_number_with_reason(number)
        is_possible = possibility in (phonenumbers.ValidationResult.IS_POSSIBLE,
                                      phonenumbers.ValidationResult.IS_POSSIBLE_LOCAL_ONLY)
        status = (
            ValidationStatus.VALID if is_valid
            else ValidationStatus.POSSIBLE_NOT_VALID if is_possible
            else ValidationStatus.INVALID
        )
        region_code = phonenumbers.region_code_for_number(number)
        number_type = _TYPE_NAMES.get(phonenumbers.number_type(number), "UNKNOWN") if is_valid else "UNKNOWN"
        carrier_name = carrier.name_for_number(number, "en") if is_valid else ""
        country_name = geocoder.country_name_for_number(number, "en") or None
        description = geocoder.description_for_number(number, "en") if is_valid else ""
        zones = [z for z in timezone.time_zones_for_number(number) if z != "Etc/Unknown"] if is_valid else []
        return PhoneIntel(
            raw_input=raw,
            e164=phonenumbers.format_number(number, PhoneNumberFormat.E164),
            international=phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL),
            national=phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL),
            rfc3966=phonenumbers.format_number(number, PhoneNumberFormat.RFC3966),
            country_code=number.country_code or 0,
            region_code=region_code if region_code not in (None, "ZZ") else None,
            country_name=country_name,
            region_description=description or None,
            number_type=number_type,
            validation_status=status,
            is_valid=is_valid,
            is_possible=is_possible,
            possibility_reason=_POSSIBILITY.get(possibility, "UNKNOWN"),
            national_significant_number=phonenumbers.national_significant_number(number),
            timezones=zones,
            carrier=carrier_name or None,
        )

    def require_investigable(self, raw: str, region: str | None = None) -> PhoneIntel:
        intel = self.analyze(raw, region)
        if not intel.is_valid:
            raise ValidationFailed(
                "Investigations require a valid phone number",
                code="invalid_phone_number",
                details=[{"validation_status": intel.validation_status.value,
                          "reason": intel.possibility_reason}],
            )
        return intel
