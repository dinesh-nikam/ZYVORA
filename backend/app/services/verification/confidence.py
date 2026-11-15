"""Transparent confidence scoring.

The score is the sum of named, documented factors so every result explains
itself.  Name or username similarity is *never* a factor: a profile can only
gain meaningful confidence from an explicit, public reference to the
investigated phone number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import ConfidenceLevel

# Points per factor (documented in docs/methodology.md).
FACTOR_POINTS: dict[str, int] = {
    "phone_on_profile_live": 65,       # live profile page / official API shows the number
    "phone_on_profile_index": 35,      # search-index snippet of the profile page itself shows the number
    "phone_on_third_party_page": 20,   # number and profile link co-occur on another public page
    "multiple_independent_sources": 10,
    "profile_exists_confirmed": 10,
    "official_api_confirmation": 5,
    "user_provided_link": 5,
    "profile_not_found": -40,
    "profile_restricted": -15,
}
CAP_WITHOUT_PHONE_REFERENCE = 25

FACTOR_DESCRIPTIONS: dict[str, str] = {
    "phone_on_profile_live": "Investigated number appears in the profile's own public content (live check)",
    "phone_on_profile_index": "Investigated number appears in a search-index snippet of the profile page itself",
    "phone_on_third_party_page": "Investigated number and this profile link appear together on another public page",
    "multiple_independent_sources": "Evidence comes from two or more independent sources",
    "profile_exists_confirmed": "Profile was confirmed to exist publicly",
    "official_api_confirmation": "Existence confirmed through the platform's official API",
    "user_provided_link": "Link was supplied by the investigator",
    "profile_not_found": "Platform reports the profile does not exist",
    "profile_restricted": "Profile is private, login-walled or otherwise restricted",
}


@dataclass(slots=True)
class ConfidenceResult:
    score: int
    level: ConfidenceLevel
    factors: list[dict[str, object]] = field(default_factory=list)


def level_for(score: int) -> ConfidenceLevel:
    if score >= 75:
        return ConfidenceLevel.HIGH
    if score >= 45:
        return ConfidenceLevel.MEDIUM
    if score >= 15:
        return ConfidenceLevel.LOW
    return ConfidenceLevel.NONE


def score(active: set[str]) -> ConfidenceResult:
    factors: list[dict[str, object]] = []
    total = 0
    for name in FACTOR_POINTS:
        if name in active:
            points = FACTOR_POINTS[name]
            total += points
            factors.append({"factor": name, "points": points, "description": FACTOR_DESCRIPTIONS[name]})
    unknown = active - FACTOR_POINTS.keys()
    if unknown:
        raise ValueError(f"unknown confidence factors: {sorted(unknown)}")
    has_phone_reference = bool(active & {"phone_on_profile_live", "phone_on_profile_index",
                                         "phone_on_third_party_page"})
    if not has_phone_reference and total > CAP_WITHOUT_PHONE_REFERENCE:
        factors.append({
            "factor": "cap_no_phone_reference",
            "points": CAP_WITHOUT_PHONE_REFERENCE - total,
            "description": "Capped: no public evidence links this profile to the investigated number",
        })
        total = CAP_WITHOUT_PHONE_REFERENCE
    total = max(0, min(100, total))
    return ConfidenceResult(score=total, level=level_for(total), factors=factors)
