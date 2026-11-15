"""Aiogram 3 handlers for all PHONEINTEL Telegram Bot commands."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from phoneintel_bot.client import BackendClient, BotAPIError

logger = logging.getLogger(__name__)
router = Router()

MAX_TELEGRAM_MSG_LEN = 4000


def split_message(text: str, max_len: int = MAX_TELEGRAM_MSG_LEN) -> list[str]:
    """Split long text into multiple chunks adhering to Telegram length constraints."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    lines = text.split("\n")
    current: list[str] = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def reply_chunks(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply(chunk)


@router.message(Command("start"))
async def start_handler(message: Message, backend: BackendClient) -> None:
    text = (
        "👋 Welcome to *PHONEINTEL Bot*!\n\n"
        "PHONEINTEL provides phone-number intelligence and public social-profile discovery.\n\n"
        "📌 *Getting Started:*\n"
        "To perform investigations and view private history, link your Telegram account:\n"
        "1. Generate a code in the PHONEINTEL web app or CLI (`phoneintel link-telegram`)\n"
        "2. Send `/link YOUR_CODE` here.\n\n"
        "Type `/help` to see all available commands."
    )
    await message.reply(text, parse_mode="Markdown")


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    text = (
        "🔍 *PHONEINTEL Bot Commands:*\n\n"
        "• `/link <CODE>` — Link your Telegram account\n"
        "• `/unlink` — Disconnect your Telegram account\n"
        "• `/validate <phone> [region]` — Check E.164 validity and numbering plan\n"
        "• `/country <phone> [region]` — Country and region information\n"
        "• `/carrier <phone> [region]` — Carrier and network operator information\n"
        "• `/profile-links <phone> [purpose]` — Discover public social profiles\n"
        "• `/search-public <phone>` — Search public web and open platforms\n"
        "• `/report <investigation_id>` — Download comprehensive investigation report\n"
        "• `/history` — View your recent investigations\n"
        "• `/delete-data <investigation_id>` — Permanently erase investigation data\n"
        "• `/privacy` — Privacy and compliance policy\n\n"
        "_Note: All investigations require a legitimate purpose and authorization basis._"
    )
    await message.reply(text, parse_mode="Markdown")


@router.message(Command("privacy"))
async def privacy_handler(message: Message) -> None:
    text = (
        "🛡️ *PHONEINTEL Privacy & Transparency Notice*\n\n"
        "1. *Public-Source Only*: PHONEINTEL only searches authorized public search APIs, official platform APIs, "
        "and robots.txt-permitted public pages. No private data or password protections are ever bypassed.\n"
        "2. *No Number Enumeration*: The bot never brute-forces numbers or tests numbers "
        "against private address books.\n"
        "3. *Non-Attribution*: The presence of a public profile URL never proves ownership by the phone holder unless "
        "explicit, verifiable public evidence links them.\n"
        "4. *User Isolation*: Each user's history and reports are strictly isolated to their own linked account.\n"
        "5. *Data Deletion*: You can permanently erase your investigation data at any time via `/delete-data <id>`."
    )
    await message.reply(text, parse_mode="Markdown")


@router.message(Command("link"))
async def link_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply("⚠️ Usage: `/link <CODE>`\nExample: `/link TG-449102`", parse_mode="Markdown")
        return

    code = args[0]
    user_id = message.from_user.id if message.from_user else 0
    try:
        data = await backend.link_account(user_id, code)
        email = data.get("email")
        await message.reply(f"✅ Successfully linked to PHONEINTEL account: *{email}*!", parse_mode="Markdown")
    except BotAPIError as exc:
        await message.reply(f"❌ Linking failed: {exc}")


@router.message(Command("unlink"))
async def unlink_handler(message: Message, backend: BackendClient) -> None:
    user_id = message.from_user.id if message.from_user else 0
    try:
        await backend.unlink_account(user_id)
        await message.reply("✅ Telegram account unlinked successfully.")
    except BotAPIError as exc:
        await message.reply(f"❌ Unlink error: {exc}")


@router.message(Command("validate"))
async def validate_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        usage_text = "⚠️ Usage: `/validate <phone> [region]`\nExample: `/validate +919876543210`"
        await message.reply(usage_text, parse_mode="Markdown")
        return

    phone = args[0]
    region = args[1] if len(args) > 1 else None
    user_id = message.from_user.id if message.from_user else 0

    try:
        res = await backend.validate_phone(user_id, phone, region)
        lines = [
            "📱 *Phone Validation Summary*",
            f"• *E.164 Standard:* `{res.get('e164')}`",
            f"• *International:* `{res.get('international')}`",
            f"• *National:* `{res.get('national')}`",
            f"• *Country:* {res.get('country_name')} (+{res.get('country_code')})",
            f"• *Region Code:* {res.get('region_code') or 'N/A'}",
            f"• *Number Type:* {res.get('number_type')}",
            f"• *Status:* {res.get('validation_status')}",
            f"• *Is Valid:* {'Yes' if res.get('is_valid') else 'No'}",
        ]
        limitations = res.get("limitations", [])
        if limitations:
            lines.append("\n⚠️ *Limitations:*")
            lines.extend(f"• {lim}" for lim in limitations)

        await reply_chunks(message, "\n".join(lines))
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Validation error: {exc}")


@router.message(Command("country"))
async def country_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        usage_text = "⚠️ Usage: `/country <phone> [region]`\nExample: `/country +12025550123`"
        await message.reply(usage_text, parse_mode="Markdown")
        return

    phone = args[0]
    region = args[1] if len(args) > 1 else None
    user_id = message.from_user.id if message.from_user else 0

    try:
        res = await backend.validate_phone(user_id, phone, region)
        text = (
            f"🌍 *Country & Regional Info:*\n"
            f"• *Number:* `{res.get('e164')}`\n"
            f"• *Country:* {res.get('country_name')} (+{res.get('country_code')})\n"
            f"• *Region Code:* {res.get('region_code') or 'N/A'}\n"
            f"• *Number Type:* {res.get('number_type')}"
        )
        await message.reply(text, parse_mode="Markdown")
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Error: {exc}")


@router.message(Command("carrier"))
async def carrier_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        usage_text = "⚠️ Usage: `/carrier <phone> [region]`\nExample: `/carrier +919876543210`"
        await message.reply(usage_text, parse_mode="Markdown")
        return

    phone = args[0]
    region = args[1] if len(args) > 1 else None
    user_id = message.from_user.id if message.from_user else 0

    try:
        res = await backend.phone_metadata(user_id, phone, region)
        tzs = res.get("timezones", [])
        text = (
            f"📡 *Carrier & Network Metadata:*\n"
            f"• *Number:* `{res.get('e164')}`\n"
            f"• *Carrier:* {res.get('carrier') or 'Unavailable / Unallocated'}\n"
            f"• *Source:* {res.get('carrier_source') or 'N/A'}\n"
            f"• *Timezones:* {', '.join(tzs) if tzs else 'N/A'}\n"
            f"• *Provider:* {res.get('metadata_provider')}"
        )
        await message.reply(text, parse_mode="Markdown")
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Error: {exc}")


@router.message(Command("profile-links"))
async def profile_links_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split(maxsplit=2)[1:]
    if not args:
        usage_text = (
            "⚠️ Usage: `/profile-links <phone> [purpose]`\n"
            "Example: `/profile-links +919876543210 Triage ticket`"
        )
        await message.reply(usage_text, parse_mode="Markdown")
        return

    phone = args[0]
    purpose = args[1] if len(args) > 1 else "Telegram bot investigation"
    user_id = message.from_user.id if message.from_user else 0

    try:
        status_msg = await message.reply("⏳ Creating investigation and initiating discovery...")
        inv = await backend.create_investigation(user_id, phone, purpose=purpose)
        inv_id = inv["id"]

        await backend.start_discovery(user_id, inv_id)

        # Poll for completion up to 30 seconds
        for _ in range(15):
            await asyncio.sleep(2)
            detail = await backend.get_investigation(user_id, inv_id)
            inv_status = detail.get("investigation", {}).get("status")
            if inv_status in ("completed", "failed", "cancelled"):
                break

        profiles_data = await backend.list_profiles(user_id, inv_id)
        profiles = profiles_data.get("items", [])

        lines = [
            f"🎯 *Investigation Results:* `{inv.get('investigation_reference')}`",
            f"• *Status:* {inv_status}",
            f"• *Profiles Discovered:* {len(profiles)}\n",
        ]

        if not profiles:
            lines.append("No public profile candidates located.")
        else:
            for p in profiles[:15]:
                v_stat = p.get("verification_status", "UNVERIFIED")
                icon = "✅" if "VERIFIED" in v_stat else ("🔹" if "CANDIDATE" in v_stat else "▫️")
                lines.append(
                    f"{icon} *{p.get('platform', '').upper()}*\n"
                    f"  URL: {p.get('canonical_url')}\n"
                    f"  Status: `{v_stat}` (Confidence: {p.get('confidence_score', 0)}/100)\n"
                    f"  Evidence: _{p.get('evidence_description')}_\n"
                )

        lines.append(f"\n_For full downloadable report, use:_ `/report {inv_id}`")
        await reply_chunks(status_msg, "\n".join(lines))
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Discovery error: {exc}")


@router.message(Command("search-public"))
async def search_public_handler(message: Message, backend: BackendClient) -> None:
    # Delegates to profile_links
    await profile_links_handler(message, backend)


@router.message(Command("history"))
async def history_handler(message: Message, backend: BackendClient) -> None:
    user_id = message.from_user.id if message.from_user else 0
    try:
        data = await backend.list_investigations(user_id, page=1, page_size=10)
        items = data.get("items", [])
        total = data.get("total", 0)

        if not items:
            await message.reply("📭 No investigations recorded for your account.")
            return

        lines = [f"📂 *Your Investigation History (Total: {total})*\n"]
        for item in items:
            lines.append(
                f"• *Ref:* `{item.get('investigation_reference')}`\n"
                f"  ID: `{item.get('id')}`\n"
                f"  Status: `{item.get('status')}` | Profiles: {item.get('profile_count', 0)}\n"
                f"  Date: {str(item.get('created_at'))[:16]}\n"
            )
        await reply_chunks(message, "\n".join(lines))
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ History error: {exc}")


@router.message(Command("report"))
async def report_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        usage_text = (
            "⚠️ Usage: `/report <investigation_id>`\n"
            "Example: `/report 11111111-1111-1111-1111-111111111111`"
        )
        await message.reply(usage_text, parse_mode="Markdown")
        return

    inv_id = args[0]
    user_id = message.from_user.id if message.from_user else 0

    try:
        report_data = await backend.get_report(user_id, inv_id)
        summary = report_data.get("summary", {})
        text = (
            f"📊 *Investigation Report Summary*\n"
            f"• *Reference:* `{report_data.get('investigation_reference')}`\n"
            f"• *Discovered Profiles:* {summary.get('discovered_urls', 0)}\n"
            f"• *Verified Public References:* {summary.get('verified_public_references', 0)}\n"
            f"• *Public Candidates:* {summary.get('public_candidates', 0)}\n"
            f"• *Unverified:* {summary.get('unverified', 0)}\n"
        )
        await message.reply(text, parse_mode="Markdown")

        # Also deliver the PDF document file
        pdf_bytes = await backend.export_report(user_id, inv_id, fmt="pdf")
        filename = f"{report_data.get('investigation_reference', 'report')}.pdf"
        file = BufferedInputFile(pdf_bytes, filename=filename)
        await message.reply_document(file, caption=f"📄 Full PDF Report: {filename}")
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Report error: {exc}")


@router.message(Command("delete-data"))
async def delete_data_handler(message: Message, backend: BackendClient) -> None:
    args = (message.text or "").split()[1:]
    if not args:
        await message.reply("⚠️ Usage: `/delete-data <investigation_id>`", parse_mode="Markdown")
        return

    inv_id = args[0]
    user_id = message.from_user.id if message.from_user else 0

    try:
        await backend.delete_investigation(user_id, inv_id)
        msg = f"🗑️ Investigation `{inv_id}` and all associated evidence have been permanently deleted."
        await message.reply(msg, parse_mode="Markdown")
    except BotAPIError as exc:
        if exc.code == "telegram_not_linked":
            await message.reply("⚠️ Please link your Telegram account first using `/link <CODE>`.")
        else:
            await message.reply(f"❌ Deletion error: {exc}")
