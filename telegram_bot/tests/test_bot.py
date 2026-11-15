"""Tests for PHONEINTEL Telegram Bot handlers and client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, Message, User
from phoneintel_bot.client import BackendClient, BotAPIError
from phoneintel_bot.config import BotSettings
from phoneintel_bot.handlers import (
    carrier_handler,
    country_handler,
    delete_data_handler,
    help_handler,
    history_handler,
    link_handler,
    privacy_handler,
    profile_links_handler,
    report_handler,
    split_message,
    start_handler,
    unlink_handler,
    validate_handler,
)


def make_message(text: str, user_id: int = 123456789) -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = user_id
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = user_id
    msg.reply = AsyncMock()
    msg.reply_document = AsyncMock()
    return msg


def make_backend() -> BackendClient:
    settings = BotSettings(
        bot_token="123456:FAKE_TOKEN_FOR_TESTING",
        api_url="http://fake-api",
        service_api_key="fake-service-key",
    )
    return BackendClient(settings)


def test_split_message() -> None:
    short_text = "Hello world"
    assert split_message(short_text, max_len=20) == ["Hello world"]

    long_text = "Line 1\nLine 2\nLine 3\nLine 4"
    chunks = split_message(long_text, max_len=14)
    assert len(chunks) >= 2
    assert "Line 1" in chunks[0]


@pytest.mark.asyncio
async def test_start_command() -> None:
    msg = make_message("/start")
    backend = make_backend()
    await start_handler(msg, backend)
    msg.reply.assert_called_once()
    assert "Welcome to *PHONEINTEL Bot*" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_help_command() -> None:
    msg = make_message("/help")
    await help_handler(msg)
    msg.reply.assert_called_once()
    assert "PHONEINTEL Bot Commands:" in msg.reply.call_args[0][0]
    assert "/validate" in msg.reply.call_args[0][0]
    assert "/profile-links" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_privacy_command() -> None:
    msg = make_message("/privacy")
    await privacy_handler(msg)
    msg.reply.assert_called_once()
    assert "Privacy & Transparency Notice" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_link_command() -> None:
    msg = make_message("/link TG-123456", user_id=999888777)
    backend = make_backend()
    backend.link_account = AsyncMock(return_value={"email": "analyst@example.org"})

    await link_handler(msg, backend)
    backend.link_account.assert_called_once_with(999888777, "TG-123456")
    msg.reply.assert_called_once()
    assert "Successfully linked" in msg.reply.call_args[0][0]
    assert "analyst@example.org" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_link_command_missing_code() -> None:
    msg = make_message("/link")
    backend = make_backend()
    await link_handler(msg, backend)
    assert "Usage: `/link <CODE>`" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_unlink_command() -> None:
    msg = make_message("/unlink", user_id=999888777)
    backend = make_backend()
    backend.unlink_account = AsyncMock(return_value={"message": "unlinked"})

    await unlink_handler(msg, backend)
    backend.unlink_account.assert_called_once_with(999888777)
    assert "unlinked successfully" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_validate_command_success() -> None:
    msg = make_message("/validate +919876543210", user_id=123)
    backend = make_backend()
    backend.validate_phone = AsyncMock(return_value={
        "e164": "+919876543210",
        "international": "+91 98765 43210",
        "national": "098765 43210",
        "country_name": "India",
        "country_code": 91,
        "region_code": "IN",
        "number_type": "MOBILE",
        "validation_status": "VALID",
        "is_valid": True,
        "limitations": ["Not proof of identity"],
    })

    await validate_handler(msg, backend)
    backend.validate_phone.assert_called_once_with(123, "+919876543210", None)
    msg.reply.assert_called_once()
    reply = msg.reply.call_args[0][0]
    assert "+919876543210" in reply
    assert "India (+91)" in reply


@pytest.mark.asyncio
async def test_validate_command_unlinked_user() -> None:
    msg = make_message("/validate +919876543210", user_id=456)
    backend = make_backend()
    backend.validate_phone = AsyncMock(side_effect=BotAPIError("Not linked", code="telegram_not_linked"))

    await validate_handler(msg, backend)
    assert "link your Telegram account first" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_country_command() -> None:
    msg = make_message("/country +12025550123", user_id=123)
    backend = make_backend()
    backend.validate_phone = AsyncMock(return_value={
        "e164": "+12025550123",
        "country_name": "United States",
        "country_code": 1,
        "region_code": "US",
        "number_type": "FIXED_LINE_OR_MOBILE",
    })

    await country_handler(msg, backend)
    assert "United States (+1)" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_carrier_command() -> None:
    msg = make_message("/carrier +919876543210", user_id=123)
    backend = make_backend()
    backend.phone_metadata = AsyncMock(return_value={
        "e164": "+919876543210",
        "carrier": "Bharti Airtel",
        "carrier_source": "twilio",
        "timezones": ["Asia/Kolkata"],
        "metadata_provider": "twilio_lookup",
    })

    await carrier_handler(msg, backend)
    assert "Bharti Airtel" in msg.reply.call_args[0][0]
    assert "Asia/Kolkata" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_profile_links_command() -> None:
    msg = make_message("/profile-links +919876543210 Fraud triage", user_id=123)
    backend = make_backend()
    backend.create_investigation = AsyncMock(return_value={"id": "inv-111", "investigation_reference": "INV-001"})
    backend.start_discovery = AsyncMock(return_value={"id": "job-1", "status": "queued"})
    backend.get_investigation = AsyncMock(return_value={"investigation": {"status": "completed"}})
    backend.list_profiles = AsyncMock(return_value={
        "items": [
            {
                "platform": "telegram",
                "canonical_url": "https://t.me/acme",
                "verification_status": "VERIFIED_PUBLIC_REFERENCE",
                "confidence_score": 85,
                "evidence_description": "Bio match",
            }
        ]
    })

    await profile_links_handler(msg, backend)
    backend.create_investigation.assert_called_once_with(123, "+919876543210", purpose="Fraud triage")


@pytest.mark.asyncio
async def test_history_command() -> None:
    msg = make_message("/history", user_id=123)
    backend = make_backend()
    backend.list_investigations = AsyncMock(return_value={
        "total": 1,
        "items": [
            {
                "id": "inv-111",
                "investigation_reference": "INV-001",
                "status": "completed",
                "profile_count": 2,
                "created_at": "2026-11-15T12:00:00Z",
            }
        ]
    })

    await history_handler(msg, backend)
    backend.list_investigations.assert_called_once_with(123, page=1, page_size=10)
    assert "INV-001" in msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_report_command() -> None:
    msg = make_message("/report inv-111", user_id=123)
    backend = make_backend()
    backend.get_report = AsyncMock(return_value={
        "investigation_reference": "INV-001",
        "summary": {
            "discovered_urls": 4,
            "verified_public_references": 2,
            "public_candidates": 1,
            "unverified": 1,
        }
    })
    backend.export_report = AsyncMock(return_value=b"%PDF-1.4 mock pdf data")

    await report_handler(msg, backend)
    backend.get_report.assert_called_once_with(123, "inv-111")
    backend.export_report.assert_called_once_with(123, "inv-111", fmt="pdf")
    msg.reply_document.assert_called_once()


@pytest.mark.asyncio
async def test_delete_data_command() -> None:
    msg = make_message("/delete-data inv-111", user_id=123)
    backend = make_backend()
    backend.delete_investigation = AsyncMock()

    await delete_data_handler(msg, backend)
    backend.delete_investigation.assert_called_once_with(123, "inv-111")
    assert "permanently deleted" in msg.reply.call_args[0][0]
