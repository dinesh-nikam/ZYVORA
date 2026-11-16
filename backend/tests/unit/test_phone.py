from __future__ import annotations

import pytest
from app.core.errors import ValidationFailed
from app.models.enums import ValidationStatus
from app.services.phone.matching import build_variants, text_references_number
from app.services.phone.service import PhoneIntelService

svc = PhoneIntelService()


@pytest.mark.parametrize(
    ("raw", "region", "e164", "cc", "iso"),
    [
        ("+919876543210", None, "+919876543210", 91, "IN"),
        ("+91 98765-43210", None, "+919876543210", 91, "IN"),
        ("098765 43210", "IN", "+919876543210", 91, "IN"),
        ("+1 (650) 253-0000", None, "+16502530000", 1, "US"),
        ("+44 20 7946 0958", None, "+442079460958", 44, "GB"),
        ("+49 30 901820", None, "+4930901820", 49, "DE"),
    ],
)
def test_normalizes_to_e164_and_detects_country(raw: str, region: str | None, e164: str, cc: int, iso: str) -> None:
    intel = svc.analyze(raw, region)
    assert intel.e164 == e164
    assert intel.country_code == cc
    assert intel.region_code == iso
    assert intel.country_name


def test_classifies_mobile_number() -> None:
    intel = svc.analyze("+919876543210")
    assert intel.validation_status is ValidationStatus.VALID
    assert intel.number_type in {"MOBILE", "FIXED_LINE_OR_MOBILE"}
    assert intel.timezones
    assert intel.metadata_provider.startswith("libphonenumber")
    assert any("ownership" in lim for lim in intel.limitations)


def test_toll_free_classification() -> None:
    assert svc.analyze("+18002345678").number_type == "TOLL_FREE"


def test_invalid_but_parseable_number() -> None:
    intel = svc.analyze("+12345")
    assert intel.validation_status in (ValidationStatus.INVALID, ValidationStatus.POSSIBLE_NOT_VALID)
    assert intel.is_valid is False
    assert intel.number_type == "UNKNOWN"
    assert intel.carrier is None


@pytest.mark.parametrize("raw", ["abc", "+91 98765 43210 ext", "<script>", "1" * 40, "", "+9+1987", "91+98765"])
def test_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(ValidationFailed):
        svc.analyze(raw)


def test_national_format_requires_region() -> None:
    with pytest.raises(ValidationFailed) as exc:
        svc.analyze("09876543210")
    assert exc.value.code == "region_required"


def test_unknown_region_rejected() -> None:
    with pytest.raises(ValidationFailed):
        svc.analyze("09876543210", "XX")


def test_require_investigable_rejects_invalid() -> None:
    with pytest.raises(ValidationFailed) as exc:
        svc.require_investigable("+12345")
    assert exc.value.code == "invalid_phone_number"


class TestNumberMatching:
    variants = build_variants("+919876543210")

    @pytest.mark.parametrize(
        "text",
        [
            "Call +91 98765 43210 today",
            "WhatsApp: +919876543210",
            "phone 098765-43210",
            "wa.me/919876543210",
            "tel:0091 98765 43210",
            "(+91) 98765.43210",
        ],
    )
    def test_detects_formats(self, text: str) -> None:
        assert text_references_number(text, self.variants)

    @pytest.mark.parametrize(
        "text",
        [
            "Call +91 98765 43211 today",  # different number
            "Order 19876543210999 shipped",  # digits embedded in a longer run
            "John Doe 9876 profile",  # partial overlap
            "",
            "+44 20 7946 0958",
        ],
    )
    def test_no_false_positives(self, text: str) -> None:
        assert not text_references_number(text, self.variants)

    def test_query_forms(self) -> None:
        assert "+919876543210" in self.variants.query_forms
        assert "9876543210" in self.variants.query_forms
