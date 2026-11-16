"""User-facing texts (Telegram HTML parse mode).  Every dynamic value must pass
through :func:`h` before it is interpolated."""

from __future__ import annotations

import html
from typing import Any

BASES: dict[str, str] = {
    "number_holder_consent": "Consent of the number holder",
    "own_number": "My own number",
    "fraud_prevention": "Fraud prevention",
    "security_incident_response": "Security incident response",
    "legal_obligation": "Legal obligation",
    "law_enforcement_request": "Law-enforcement request",
    "journalistic_research": "Journalistic research",
    "authorized_penetration_test": "Authorised penetration test",
}

STATUS_ICON = {
    "VERIFIED_PUBLIC_REFERENCE": "✅",
    "PUBLIC_CANDIDATE": "🟡",
    "UNVERIFIED": "⚪",
    "INACCESSIBLE": "🚫",
    "NOT_FOUND": "❌",
    "RESTRICTED": "🔒",
    "ERROR": "⚠️",
}


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


START = (
    "<b>PHONEINTEL</b> - phone-number intelligence and <b>public</b> social-profile link discovery.\n\n"
    "To use the bot, link it to your PHONEINTEL account:\n"
    "1. Run <code>phoneintel link-telegram</code> (or call the API) to get a one-time code.\n"
    "2. Send <code>/link CODE</code> here.\n\n"
    "Every investigation needs a stated purpose and authorization basis. /help lists all commands, "
    "/privacy explains what the bot does and does not do."
)

HELP = (
    "<b>Commands</b>\n"
    "/validate &lt;number&gt; - parse and validate a number (E.164)\n"
    "/country &lt;number&gt; - country / region of a number\n"
    "/carrier &lt;number&gt; - carrier (allocation data) and number type\n"
    "/profile_links &lt;number&gt; - discover evidence-backed public profile links\n"
    "/search_public &lt;number&gt; - public web index search only (no live checks)\n"
    "/report [id|reference] [pdf|json|csv|html] - download a report (default: latest, PDF)\n"
    "/history - your recent investigations\n"
    "/cancel - abort the current dialog or running discovery\n"
    "/delete_data [id] - erase one investigation, or all of your investigation data\n"
    "/link CODE · /unlink - connect or disconnect your PHONEINTEL account\n"
    "/privacy - privacy notice\n\n"
    "Hyphenated forms such as /profile-links also work. Numbers must be in international format (+...)."
)

PRIVACY = (
    "<b>Privacy &amp; limitations</b>\n"
    "• Only authorised public search APIs, official platform APIs, robots-permitted public pages and links you "
    "supply are used. Logins, privacy settings, CAPTCHAs and rate limits are never bypassed.\n"
    "• No phone-number-to-account lookups (contact discovery) are made on any platform; WhatsApp and Telegram "
    "registration status is never checked.\n"
    "• A public reference shows that a number was <i>published</i> alongside a profile - it does not prove who "
    "controls the profile. Name similarity is never used as evidence.\n"
    "• You only ever see your own investigations. Every access is audit-logged.\n"
    "• Data is kept for a limited retention period; /delete_data erases it immediately.\n"
    "• This bot works only in private chats."
)

NOT_LINKED = (
    "Your Telegram account is not linked to a PHONEINTEL account yet. Create a code with "
    "<code>phoneintel link-telegram</code> and send <code>/link CODE</code>."
)

ACK = (
    "I confirm the authorization basis above applies to this investigation, and I understand the results are "
    "public-source indications, not proof of identity or ownership."
)


def error_text(code: str | None, message: str, details: Any = None) -> str:
    if code == "telegram_not_linked":
        return NOT_LINKED
    if code == "rate_limited":
        return "The service rate limit was reached. Please try again later."
    if code in ("backend_unavailable", "timeout"):
        return "The PHONEINTEL service is temporarily unavailable. Please try again later."
    text = f"⚠️ {h(message)}"
    if code in ("validation_error", "invalid_phone_number", "invalid_phone_input", "region_required") and details:
        text += "\n" + "\n".join(
            f"• {h(d.get('msg') if isinstance(d, dict) and 'msg' in d else d)}" for d in details[:5]
        )
    return text


def phone_summary(data: dict[str, Any], *, title: str, fields: list[tuple[str, str]]) -> str:
    lines = [f"<b>{h(title)}</b>"]
    for label, key in fields:
        value = data.get(key)
        if isinstance(value, list):
            value = ", ".join(map(str, value))
        lines.append(f"{h(label)}: <code>{h(value if value not in (None, '') else '-')}</code>")
    if data.get("limitations"):
        lines.append("\n<i>" + h(data["limitations"][0]) + "</i>")
    return "\n".join(lines)


def results_text(investigation: dict[str, Any], profiles: list[dict[str, Any]], limit: int = 15) -> str:
    summary = investigation.get("summary") or {}
    counts = summary.get("counts") or {}
    lines = [
        f"<b>Investigation {h(investigation['investigation_reference'])}</b> - {h(investigation['status'])}",
        f"Profiles: {counts.get('total_profiles', len(profiles))} · verified "
        f"{counts.get('VERIFIED_PUBLIC_REFERENCE', 0)} · candidates {counts.get('PUBLIC_CANDIDATE', 0)} · "
        f"unverified {counts.get('UNVERIFIED', 0)}",
        "",
    ]
    if not profiles:
        lines.append("No profile links were found in permitted public sources.")
    for p in profiles[:limit]:
        icon = STATUS_ICON.get(p["verification_status"], "•")
        lines.append(
            f"{icon} <b>{h(p['platform'])}</b> {h(p['canonical_url'])}\n"
            f"   {h(p['verification_status'])} · {h(p['confidence_level'])} ({p['confidence_score']}/100)\n"
            f"   <i>{h(p['evidence_description'])}</i>"
        )
    if len(profiles) > limit:
        lines.append(f"… and {len(profiles) - limit} more in the report.")
    if summary.get("limitations"):
        lines.append("\n<i>" + h(summary["limitations"][0]) + "</i>")
    return "\n".join(lines)


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split on line boundaries so no chunk exceeds Telegram's message limit."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for raw_line in text.split("\n"):
        line = raw_line
        while len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
