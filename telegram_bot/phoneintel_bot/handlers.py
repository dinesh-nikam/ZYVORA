"""aiogram 3 handlers.

All business logic lives in the PHONEINTEL backend; the bot only collects
input (including the mandatory authorization basis, purpose and
acknowledgement), shows progress, and delivers results/reports.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from phoneintel_bot import texts
from phoneintel_bot.client import BackendClient, BotAPIError
from phoneintel_bot.config import BotSettings
from phoneintel_bot.texts import h

logger = logging.getLogger(__name__)
router = Router(name="phoneintel")

TERMINAL = {"completed", "failed", "cancelled"}
REPORT_FORMATS = ("pdf", "json", "csv", "html")


# ------------------------------------------------------------- callback data
class BasisCB(CallbackData, prefix="basis"):
    basis: str


class FlowCB(CallbackData, prefix="flow"):
    action: str  # confirm | abort


class JobCB(CallbackData, prefix="job"):
    action: str  # cancel
    inv: str


class ReportCB(CallbackData, prefix="rep"):
    inv: str
    fmt: str


class EraseCB(CallbackData, prefix="erase"):
    action: str  # confirm | abort
    target: str  # "all" or an investigation id


class Discovery(StatesGroup):
    basis = State()
    purpose = State()
    confirm = State()


# ------------------------------------------------------------ progress jobs
class ProgressTracker:
    """Polls a discovery job and keeps one Telegram message updated."""

    def __init__(self, backend: BackendClient, settings: BotSettings) -> None:
        self.backend = backend
        self.settings = settings
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self.running: dict[int, str] = {}  # telegram user id -> investigation id

    def start(self, bot: Bot, user_id: int, chat_id: int, message_id: int, investigation_id: str) -> None:
        previous = self._tasks.get(user_id)
        if previous is not None and not previous.done():
            previous.cancel()
        self.running[user_id] = investigation_id
        task = asyncio.create_task(
            self._track(bot, user_id, chat_id, message_id, investigation_id), name=f"track-{investigation_id}"
        )
        self._tasks[user_id] = task

        def _done(t: asyncio.Task[None], uid: int = user_id) -> None:
            if self._tasks.get(uid) is t:
                self._tasks.pop(uid, None)
                self.running.pop(uid, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error("progress tracker crashed", exc_info=t.exception())

        task.add_done_callback(_done)

    async def wait_idle(self, timeout: float = 60) -> None:
        if self._tasks:
            await asyncio.wait_for(asyncio.gather(*self._tasks.values(), return_exceptions=True), timeout)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _edit(
        self, bot: Bot, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramAPIError as exc:  # e.g. "message is not modified"
            logger.debug("progress edit skipped: %s", type(exc).__name__)

    async def _track(self, bot: Bot, uid: int, chat_id: int, message_id: int, inv_id: str) -> None:
        started = time.monotonic()
        cancel_kb = InlineKeyboardBuilder()
        cancel_kb.button(text="✖ Cancel discovery", callback_data=JobCB(action="cancel", inv=inv_id))
        last = ""
        detail: dict[str, Any] = {}
        while time.monotonic() - started < self.settings.discovery_timeout_seconds:
            try:
                detail = await self.backend.get_investigation(uid, inv_id)
            except BotAPIError as exc:
                await self._edit(bot, chat_id, message_id, texts.error_text(exc.code, exc.message))
                return
            inv = detail["investigation"]
            job = detail.get("latest_job") or {}
            if inv["status"] in TERMINAL:
                break
            text = (
                f"🔎 <b>{h(inv['investigation_reference'])}</b>\n"
                f"Status: {h(inv['status'])} · job {h(job.get('status', '?'))}"
                + (f" · retry {job.get('retry_count')}" if job.get("retry_count") else "")
                + f"\nElapsed: {int(time.monotonic() - started)} s"
            )
            if text != last:
                await self._edit(bot, chat_id, message_id, text, cancel_kb.as_markup())
                last = text
            await asyncio.sleep(self.settings.poll_interval_seconds)
        else:
            await self._edit(
                bot, chat_id, message_id, "⏳ Discovery is still running. Check later with /history or /report."
            )
            return

        inv = detail["investigation"]
        if inv["status"] != "completed":
            job = detail.get("latest_job") or {}
            await self._edit(
                bot,
                chat_id,
                message_id,
                f"Investigation {h(inv['investigation_reference'])} ended: <b>{h(inv['status'])}</b>"
                + (f" ({h(job.get('error_code'))})" if job.get("error_code") else ""),
            )
            return
        await self._edit(bot, chat_id, message_id, f"✔ Discovery finished for {h(inv['investigation_reference'])}.")
        try:
            profiles = (await self.backend.list_profiles(uid, inv_id))["items"]
        except BotAPIError as exc:
            await bot.send_message(chat_id, texts.error_text(exc.code, exc.message))
            return
        chunks = texts.split_message(texts.results_text(inv, profiles))
        for i, chunk in enumerate(chunks):
            markup = report_keyboard(inv_id) if i == len(chunks) - 1 else None
            await bot.send_message(chat_id, chunk, reply_markup=markup, disable_web_page_preview=True)


def report_keyboard(inv_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for fmt in REPORT_FORMATS:
        kb.button(text=f"📄 {fmt.upper()}", callback_data=ReportCB(inv=inv_id, fmt=fmt))
    kb.adjust(4)
    return kb.as_markup()


async def _fail(message: Message, exc: BotAPIError) -> None:
    await message.answer(texts.error_text(exc.code, exc.message, exc.details))


def _uid(message: Message) -> int:
    if message.from_user is None:  # filtered out by AccessMiddleware; defensive
        raise RuntimeError("message without sender")
    return message.from_user.id


def _args(command: CommandObject | None) -> list[str]:
    return (command.args or "").split() if command else []


# --------------------------------------------------------------- basics
@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(texts.PRIVACY)


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject, backend: BackendClient) -> None:
    args = _args(command)
    if len(args) != 1:
        await message.answer(
            "Usage: <code>/link CODE</code> (create the code with <code>phoneintel link-telegram</code>)"
        )
        return
    try:
        user = await backend.link_account(_uid(message), args[0])
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    finally:
        try:  # the code is single-use, but do not leave it in the chat history
            await message.delete()
        except TelegramAPIError as exc:
            logger.warning("could not delete /link message: %s", type(exc).__name__)
    await message.answer(f"✅ Linked to PHONEINTEL account <b>{h(user['email'])}</b>.")


@router.message(Command("unlink"))
async def cmd_unlink(message: Message, backend: BackendClient) -> None:
    try:
        result = await backend.unlink_account(_uid(message))
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    await message.answer("Unlinked." if result.get("message") == "unlinked" else "This account was not linked.")


# ----------------------------------------------------- phone intelligence
async def _phone_command(message: Message, command: CommandObject, backend: BackendClient, kind: str) -> None:
    args = _args(command)
    if not args:
        await message.answer(f"Usage: <code>/{h(command.command)} +919876543210</code>")
        return
    phone, region = (" ".join(args), None)
    if len(args) == 2 and len(args[1]) == 2 and args[1].isalpha():
        phone, region = args[0], args[1].upper()
    uid = _uid(message)
    try:
        if kind == "validate":
            data = await backend.validate_phone(uid, phone, region)
            text = texts.phone_summary(
                data,
                title="Phone validation",
                fields=[
                    ("E.164", "e164"),
                    ("International", "international"),
                    ("Country", "country_name"),
                    ("Country code", "country_code"),
                    ("Number type", "number_type"),
                    ("Validation", "validation_status"),
                ],
            )
        else:
            data = await backend.phone_metadata(uid, phone, region)
            fields = (
                [
                    ("E.164", "e164"),
                    ("Country", "country_name"),
                    ("Country code", "country_code"),
                    ("Region", "region_code"),
                    ("Region description", "region_description"),
                    ("Time zones", "timezones"),
                ]
                if kind == "country"
                else [
                    ("E.164", "e164"),
                    ("Carrier", "carrier"),
                    ("Carrier source", "carrier_source"),
                    ("Number type", "number_type"),
                    ("Validation", "validation_status"),
                ]
            )
            text = texts.phone_summary(data, title=kind.capitalize(), fields=fields)
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    await message.answer(text)


@router.message(Command("validate"))
async def cmd_validate(message: Message, command: CommandObject, backend: BackendClient) -> None:
    await _phone_command(message, command, backend, "validate")


@router.message(Command("country"))
async def cmd_country(message: Message, command: CommandObject, backend: BackendClient) -> None:
    await _phone_command(message, command, backend, "country")


@router.message(Command("carrier"))
async def cmd_carrier(message: Message, command: CommandObject, backend: BackendClient) -> None:
    await _phone_command(message, command, backend, "carrier")


# ------------------------------------------------------------- discovery
async def _begin_discovery(
    message: Message, command: CommandObject, state: FSMContext, backend: BackendClient, mode: str
) -> None:
    args = _args(command)
    if not args:
        await message.answer(f"Usage: <code>/{h(command.command)} +919876543210</code>")
        return
    try:
        intel = await backend.validate_phone(_uid(message), " ".join(args))
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    if not intel.get("is_valid"):
        await message.answer(
            f"⚠️ {h(intel.get('e164'))} is not a valid number "
            f"({h(intel.get('validation_status'))}); investigations require a valid number."
        )
        return
    await state.clear()
    await state.set_state(Discovery.basis)
    await state.update_data(phone=intel["e164"], mode=mode)
    kb = InlineKeyboardBuilder()
    for key, label in texts.BASES.items():
        kb.button(text=label, callback_data=BasisCB(basis=key))
    kb.button(text="Cancel", callback_data=FlowCB(action="abort"))
    kb.adjust(1)
    await message.answer(
        f"Investigating <code>{h(intel['e164'])}</code> ({h(intel.get('country_name'))}).\n\n"
        "<b>Step 1/3 - authorization basis.</b> Which basis authorises this investigation?",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("profile_links", "profile-links", "profilelinks"))
async def cmd_profile_links(
    message: Message, command: CommandObject, state: FSMContext, backend: BackendClient
) -> None:
    await _begin_discovery(message, command, state, backend, "profile")


@router.message(Command("search_public", "search-public", "searchpublic"))
async def cmd_search_public(
    message: Message, command: CommandObject, state: FSMContext, backend: BackendClient
) -> None:
    await _begin_discovery(message, command, state, backend, "search")


@router.callback_query(Discovery.basis, BasisCB.filter())
async def on_basis(query: CallbackQuery, callback_data: BasisCB, state: FSMContext, bot: Bot) -> None:
    if callback_data.basis not in texts.BASES:
        await query.answer("Unknown basis", show_alert=True)
        return
    await state.update_data(basis=callback_data.basis)
    await state.set_state(Discovery.purpose)
    await query.answer()
    if query.message is not None:
        await bot.send_message(
            query.message.chat.id,
            f"Basis: <b>{h(texts.BASES[callback_data.basis])}</b>\n\n<b>Step 2/3 - purpose.</b> Describe why this "
            "investigation is necessary (10-1000 characters, e.g. a ticket or case reference). /cancel aborts.",
        )


@router.message(Discovery.purpose, F.text, ~F.text.startswith("/"))
async def on_purpose(message: Message, state: FSMContext) -> None:
    purpose = (message.text or "").strip()
    if not 10 <= len(purpose) <= 1000:
        await message.answer("The purpose must be between 10 and 1000 characters. Please try again.")
        return
    data = await state.update_data(purpose=purpose)
    await state.set_state(Discovery.confirm)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ I confirm - start", callback_data=FlowCB(action="confirm"))
    kb.button(text="Cancel", callback_data=FlowCB(action="abort"))
    kb.adjust(1)
    mode = "public web index search only" if data["mode"] == "search" else "public profile-link discovery"
    await message.answer(
        f"<b>Step 3/3 - confirm</b>\nNumber: <code>{h(data['phone'])}</code>\nMode: {mode}\n"
        f"Basis: {h(texts.BASES[data['basis']])}\nPurpose: {h(purpose)}\n\n<i>{h(texts.ACK)}</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    StateFilter(Discovery.basis, Discovery.purpose, Discovery.confirm), FlowCB.filter(F.action == "abort")
)
async def on_abort(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await query.answer("Cancelled")
    if query.message is not None:
        await bot.send_message(query.message.chat.id, "Cancelled. Nothing was created.")


@router.callback_query(Discovery.confirm, FlowCB.filter(F.action == "confirm"))
async def on_confirm(
    query: CallbackQuery, state: FSMContext, backend: BackendClient, tracker: ProgressTracker, bot: Bot
) -> None:
    data = await state.get_data()
    await state.clear()  # drop the number from FSM storage immediately
    uid = query.from_user.id
    await query.answer("Starting…")
    message = query.message
    if message is None:
        return
    try:
        inv = await backend.create_investigation(uid, phone=data["phone"], purpose=data["purpose"], basis=data["basis"])
        await backend.start_discovery(
            uid, inv["id"], live_checks=data["mode"] != "search", idempotency_key=f"tg-query-{query.id}"[:128]
        )
    except BotAPIError as exc:
        await bot.send_message(message.chat.id, texts.error_text(exc.code, exc.message, exc.details))
        return
    progress = await bot.send_message(
        message.chat.id, f"🔎 Investigation <b>{h(inv['investigation_reference'])}</b> created; discovery queued…"
    )
    tracker.start(bot, uid, message.chat.id, progress.message_id, inv["id"])


@router.callback_query(JobCB.filter(F.action == "cancel"))
async def on_cancel_job(query: CallbackQuery, callback_data: JobCB, backend: BackendClient) -> None:
    try:
        await backend.cancel_investigation(query.from_user.id, callback_data.inv)
    except BotAPIError as exc:
        await query.answer(exc.message[:180], show_alert=True)
        return
    await query.answer("Cancellation requested")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, backend: BackendClient, tracker: ProgressTracker) -> None:
    if await state.get_state() is not None:
        await state.clear()
        await message.answer("Dialog cancelled. Nothing was created.")
        return
    inv_id = tracker.running.get(_uid(message))
    if inv_id is None:
        await message.answer("Nothing to cancel.")
        return
    try:
        await backend.cancel_investigation(_uid(message), inv_id)
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    await message.answer("Cancellation requested.")


@router.message(StateFilter(Discovery.basis, Discovery.confirm), F.text, ~F.text.startswith("/"))
async def on_unexpected_text(message: Message) -> None:
    await message.answer("Please use the buttons above, or /cancel.")


# ---------------------------------------------------------------- reports
async def _send_report(bot: Bot, chat_id: int, uid: int, backend: BackendClient, inv_id: str, fmt: str) -> None:
    try:
        report = await backend.get_report(uid, inv_id)
        content, filename = await backend.export_report(uid, inv_id, fmt)
    except BotAPIError as exc:
        await bot.send_message(chat_id, texts.error_text(exc.code, exc.message))
        return
    s = report["summary"]
    caption = (
        f"<b>{h(report['investigation_reference'])}</b> · {s['discovered_urls']} URL(s) · "
        f"{s['verified_public_references']} verified · {s['public_candidates']} candidates"
    )
    await bot.send_document(
        chat_id, BufferedInputFile(content, filename=filename), caption=caption, protect_content=True
    )


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject, backend: BackendClient, bot: Bot) -> None:
    args = _args(command)
    fmt = "pdf"
    if args and args[-1].lower() in REPORT_FORMATS:
        fmt = args.pop().lower()
    uid = _uid(message)
    try:
        inv_id = await backend.resolve_investigation(uid, args[0] if args else None)
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    if inv_id is None:
        await message.answer("No matching investigation found.")
        return
    await _send_report(bot, message.chat.id, uid, backend, inv_id, fmt)


@router.callback_query(ReportCB.filter())
async def on_report(query: CallbackQuery, callback_data: ReportCB, backend: BackendClient, bot: Bot) -> None:
    if callback_data.fmt not in REPORT_FORMATS or query.message is None:
        await query.answer("Unsupported format", show_alert=True)
        return
    await query.answer("Preparing report…")
    await _send_report(bot, query.message.chat.id, query.from_user.id, backend, callback_data.inv, callback_data.fmt)


@router.message(Command("history"))
async def cmd_history(message: Message, backend: BackendClient) -> None:
    try:
        data = await backend.list_investigations(_uid(message), page_size=10)
    except BotAPIError as exc:
        await _fail(message, exc)
        return
    items = data.get("items") or []
    if not items:
        await message.answer("You have no investigations.")
        return
    lines = [f"<b>Your investigations</b> ({data.get('total', len(items))} total, newest first)"]
    for item in items:
        lines.append(
            f"• <code>{h(item['investigation_reference'])}</code> {h(item['status'])} · "
            f"{item.get('profile_count', 0)} profile(s) · {h(str(item['created_at'])[:16].replace('T', ' '))}"
        )
    lines.append("\n/report REFERENCE downloads a report.")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------- erasure
@router.message(Command("delete_data", "delete-data", "deletedata"))
async def cmd_delete_data(message: Message, command: CommandObject, backend: BackendClient) -> None:
    args = _args(command)
    uid = _uid(message)
    target = "all"
    what = "ALL of your investigation data"
    if args:
        try:
            inv_id = await backend.resolve_investigation(uid, args[0])
        except BotAPIError as exc:
            await _fail(message, exc)
            return
        if inv_id is None:
            await message.answer("No matching investigation found.")
            return
        target, what = inv_id, f"investigation <code>{h(args[0])}</code>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Yes, erase permanently", callback_data=EraseCB(action="confirm", target=target))
    kb.button(text="Cancel", callback_data=EraseCB(action="abort", target=target))
    kb.adjust(1)
    await message.answer(f"Permanently erase {what}? This cannot be undone.", reply_markup=kb.as_markup())


@router.callback_query(EraseCB.filter())
async def on_erase(query: CallbackQuery, callback_data: EraseCB, backend: BackendClient, bot: Bot) -> None:
    if callback_data.action != "confirm":
        await query.answer("Nothing was erased")
        return
    try:
        if callback_data.target == "all":
            result = await backend.erase_all(query.from_user.id)
            text = f"🗑 Erased {result.get('investigations_erased', 0)} investigation(s)."
        else:
            await backend.delete_investigation(query.from_user.id, callback_data.target)
            text = "🗑 Investigation erased."
    except BotAPIError as exc:
        await query.answer(exc.message[:180], show_alert=True)
        return
    await query.answer("Erased")
    if query.message is not None:
        await bot.send_message(query.message.chat.id, text)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Unknown command. See /help.")
