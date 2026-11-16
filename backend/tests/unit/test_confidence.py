from __future__ import annotations

import pytest
from app.models.enums import ConfidenceLevel
from app.services.verification.confidence import CAP_WITHOUT_PHONE_REFERENCE, FACTOR_POINTS, level_for, score


def test_live_reference_is_high() -> None:
    result = score({"phone_on_profile_live", "profile_exists_confirmed", "official_api_confirmation"})
    assert result.level is ConfidenceLevel.HIGH
    assert result.score == 80


def test_index_only_reference_is_medium_at_most() -> None:
    result = score({"phone_on_profile_index", "multiple_independent_sources"})
    assert result.level is ConfidenceLevel.MEDIUM


def test_third_party_cooccurrence_is_low() -> None:
    assert score({"phone_on_third_party_page"}).level is ConfidenceLevel.LOW


def test_without_phone_reference_score_is_capped() -> None:
    result = score(
        {"profile_exists_confirmed", "official_api_confirmation", "user_provided_link", "multiple_independent_sources"}
    )
    assert result.score <= CAP_WITHOUT_PHONE_REFERENCE
    assert result.level in (ConfidenceLevel.LOW, ConfidenceLevel.NONE)
    assert any(f["factor"] == "cap_no_phone_reference" for f in result.factors)


def test_name_similarity_is_not_a_factor() -> None:
    assert not any("name" in key for key in FACTOR_POINTS)
    with pytest.raises(ValueError, match="unknown confidence factors"):
        score({"name_similarity"})


def test_not_found_drives_score_to_floor() -> None:
    assert score({"profile_not_found"}).score == 0


def test_every_factor_is_explained() -> None:
    result = score(set(FACTOR_POINTS) - {"profile_not_found", "profile_restricted"})
    assert all(f["description"] for f in result.factors)
    assert result.score == 100


@pytest.mark.parametrize(
    ("value", "level"),
    [(100, "HIGH"), (75, "HIGH"), (74, "MEDIUM"), (45, "MEDIUM"), (44, "LOW"), (15, "LOW"), (14, "NONE"), (0, "NONE")],
)
def test_level_boundaries(value: int, level: str) -> None:
    assert level_for(value).value == level
