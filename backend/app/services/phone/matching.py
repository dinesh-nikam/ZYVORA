"""Detect explicit references to a specific phone number inside free text.

Only exact number matches count (any common formatting).  Partial digit
overlaps, names or other similarity signals never count as a reference.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberMatcher

logger = logging.getLogger(__name__)

_DIGIT_RUN = re.compile(r"(?<![\w])\+?\(?\d[\d\s().\-/]{5,}\d(?![\w])")


@dataclass(frozen=True, slots=True)
class NumberVariants:
    e164: str
    country_code: int
    nsn: str  # national significant number
    region: str | None
    digit_forms: frozenset[str]
    query_forms: tuple[str, ...]


def build_variants(e164: str) -> NumberVariants:
    number = phonenumbers.parse(e164, None)
    nsn = phonenumbers.national_significant_number(number)
    cc = str(number.country_code)
    region = phonenumbers.region_code_for_number(number)
    national_digits = re.sub(r"\D", "", phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL))
    digit_forms = {cc + nsn, "00" + cc + nsn, national_digits}
    if len(nsn) >= 8:
        digit_forms.add(nsn)
    international = phonenumbers.format_number(number, PhoneNumberFormat.INTERNATIONAL)
    query_forms = tuple(dict.fromkeys([f"+{cc}{nsn}", international, nsn if len(nsn) >= 8 else international]))
    return NumberVariants(
        e164=f"+{cc}{nsn}",
        country_code=number.country_code or 0,
        nsn=nsn,
        region=region if region not in (None, "ZZ") else None,
        digit_forms=frozenset(d for d in digit_forms if len(d) >= 7),
        query_forms=query_forms,
    )


def text_references_number(text: str | None, variants: NumberVariants) -> bool:
    """True if ``text`` explicitly contains the number in any common format."""
    if not text:
        return False
    sample = text[:200_000]
    # 1) libphonenumber's matcher (handles national/international formats).
    try:
        for match in PhoneNumberMatcher(sample, variants.region or "ZZ", leniency=phonenumbers.Leniency.POSSIBLE):
            if (
                match.number.country_code == variants.country_code
                and phonenumbers.national_significant_number(match.number) == variants.nsn
            ):
                return True
    except Exception:
        logger.debug("PhoneNumberMatcher failed; using digit-run fallback", exc_info=True)
    # 2) Strict digit-run comparison as a fallback (e.g. "wa.me/919876543210").
    for run in _DIGIT_RUN.findall(sample):
        digits = re.sub(r"\D", "", run)
        if digits in variants.digit_forms:
            return True
    return any(embedded in variants.digit_forms for embedded in re.findall(r"(?<!\d)\d{7,15}(?!\d)", sample))
