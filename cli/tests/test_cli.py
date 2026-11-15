"""Tests for PHONEINTEL CLI."""

from __future__ import annotations

import json
from unittest.mock import patch

from phoneintel_cli.client import APIError
from phoneintel_cli.main import app
from typer.testing import CliRunner

runner = CliRunner(env={"COLUMNS": "200"})


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "PHONEINTEL:" in result.stdout
    assert "validate" in result.stdout
    assert "country" in result.stdout
    assert "carrier" in result.stdout
    assert "profile-links" in result.stdout
    assert "public-search" in result.stdout
    assert "verify-url" in result.stdout
    assert "report" in result.stdout
    assert "history" in result.stdout
    assert "delete-data" in result.stdout


def test_validate_success() -> None:
    mock_data = {
        "e164": "+919876543210",
        "international": "+91 98765 43210",
        "national": "098765 43210",
        "country_code": 91,
        "region_code": "IN",
        "country_name": "India",
        "number_type": "MOBILE",
        "validation_status": "VALID",
        "is_valid": True,
        "is_possible": True,
        "limitations": ["Attribution Warning: Number validity does not prove account ownership"],
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.validate_phone", return_value=mock_data):
        result = runner.invoke(app, ["validate", "+919876543210"])
        assert result.exit_code == 0
        assert "Phone Validation Summary" in result.stdout
        assert "India" in result.stdout
        assert "VALID" in result.stdout


def test_validate_json_output() -> None:
    mock_data = {
        "e164": "+919876543210",
        "country_code": 91,
        "validation_status": "VALID",
        "is_valid": True,
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.validate_phone", return_value=mock_data):
        result = runner.invoke(app, ["validate", "+919876543210", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert parsed["e164"] == "+919876543210"
        assert parsed["is_valid"] is True


def test_validate_api_error() -> None:
    with patch("phoneintel_cli.main.PhoneIntelClient.validate_phone", side_effect=APIError("Invalid number format")):
        result = runner.invoke(app, ["validate", "invalid"])
        assert result.exit_code == 1
        assert "Error:" in result.stdout or "Error:" in result.stderr
        assert "Invalid number format" in (result.stdout + result.stderr)


def test_country_command() -> None:
    mock_data = {
        "e164": "+12025550123",
        "country_name": "United States",
        "country_code": 1,
        "region_code": "US",
        "number_type": "FIXED_LINE_OR_MOBILE",
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.validate_phone", return_value=mock_data):
        result = runner.invoke(app, ["country", "+12025550123"])
        assert result.exit_code == 0
        assert "United States (+1)" in result.stdout
        assert "US" in result.stdout


def test_carrier_command() -> None:
    mock_data = {
        "e164": "+919876543210",
        "country_name": "India",
        "country_code": 91,
        "carrier": "Airtel India",
        "carrier_source": "libphonenumber",
        "metadata_provider": "local numbering plan",
        "number_type": "MOBILE",
        "timezones": ["Asia/Kolkata"],
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.phone_metadata", return_value=mock_data):
        result = runner.invoke(app, ["carrier", "+919876543210"])
        assert result.exit_code == 0
        assert "Airtel India" in result.stdout
        assert "Asia/Kolkata" in result.stdout


def test_verify_url_command() -> None:
    mock_data = {
        "input_url": "https://t.me/example_user",
        "canonical_url": "https://t.me/example_user",
        "platform": "telegram",
        "platform_name": "Telegram",
        "handle": "example_user",
        "verification_status": "PUBLIC_CANDIDATE",
        "confidence_score": 35,
        "confidence_level": "LOW",
        "evidence_description": "Public channel exists, no direct phone reference located",
        "stages": [
            {"stage": "url_syntax", "outcome": "passed", "detail": "Valid URL"},
            {"stage": "domain_allowlist", "outcome": "passed", "detail": "t.me allowlisted"},
        ],
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.verify_url", return_value=mock_data):
        result = runner.invoke(app, ["verify-url", "https://t.me/example_user"])
        assert result.exit_code == 0
        assert "URL Verification Result" in result.stdout
        assert "Telegram" in result.stdout
        assert "PUBLIC_CANDIDATE" in result.stdout


def test_profile_links_command() -> None:
    mock_inv = {"id": "11111111-1111-1111-1111-111111111111", "investigation_reference": "INV-202611-0001"}
    mock_job = {"id": "22222222-2222-2222-2222-222222222222", "status": "queued"}
    mock_status = {"investigation": {"status": "completed"}}
    mock_profiles = {
        "items": [
            {
                "platform": "telegram",
                "canonical_url": "https://t.me/acme",
                "verification_status": "VERIFIED_PUBLIC_REFERENCE",
                "confidence_level": "HIGH",
                "confidence_score": 85,
                "evidence_description": "Found phone +919876543210 on public channel bio",
            }
        ]
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.create_investigation", return_value=mock_inv), \
         patch("phoneintel_cli.main.PhoneIntelClient.start_discovery", return_value=mock_job), \
         patch("phoneintel_cli.main.PhoneIntelClient.get_investigation", return_value=mock_status), \
         patch("phoneintel_cli.main.PhoneIntelClient.list_profiles", return_value=mock_profiles):
        result = runner.invoke(app, ["profile-links", "+919876543210"])
        assert result.exit_code == 0
        assert "INV-202611-0001" in result.stdout
        assert "VERIFIED_PUBLIC_REFERENCE" in result.stdout
        assert "https://t.me/acme" in result.stdout


def test_history_command() -> None:
    mock_history = {
        "total": 1,
        "items": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "investigation_reference": "INV-202611-0001",
                "normalized_phone": "+919876543210",
                "authorization_basis": "fraud_prevention",
                "status": "completed",
                "profile_count": 3,
                "created_at": "2026-11-15T12:00:00Z",
            }
        ]
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.list_investigations", return_value=mock_history):
        result = runner.invoke(app, ["history"])
        assert result.exit_code == 0
        assert "Investigation History (Total: 1)" in result.stdout
        assert "INV-202611-0001" in result.stdout
        assert "fraud_prevention" in result.stdout


def test_delete_data_command() -> None:
    with patch("phoneintel_cli.main.PhoneIntelClient.delete_investigation") as mock_del:
        result = runner.invoke(app, ["delete-data", "11111111-1111-1111-1111-111111111111", "--yes"])
        assert result.exit_code == 0
        assert "permanently deleted" in result.stdout
        mock_del.assert_called_once_with("11111111-1111-1111-1111-111111111111")


def test_link_telegram_command() -> None:
    mock_code = {
        "code": "TG-449102",
        "expires_in": 600,
        "instructions": "Send '/link TG-449102' to the bot",
    }
    with patch("phoneintel_cli.main.PhoneIntelClient.create_telegram_link_code", return_value=mock_code):
        result = runner.invoke(app, ["link-telegram"])
        assert result.exit_code == 0
        assert "TG-449102" in result.stdout
        assert "Send '/link TG-449102'" in result.stdout
