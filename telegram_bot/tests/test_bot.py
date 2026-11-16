"""Telegram bot tests: real aiogram Dispatcher + handlers + middlewares, real
PHONEINTEL backend (uvicorn thread), and a recording Telegram session in place
of api.telegram.org."""

from __future__ import annotations

import itertools
import time
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import DeleteMessage, EditMessageText, SendDocument, SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User
from phoneintel_bot.client import BackendClient
from phoneintel_bot.config import BotSettings
from phoneintel_bot.main import build_dispatcher
from phoneintel_bot.middlewares import MemoryRateLimiter
from phoneintel_bot.texts import split_message
from tests.live_server import LiveBackend

PASSWORD = "Correct-Horse-Battery-9"


class RecordingSession(BaseSession):
    """Stands in for api.telegram.org: records every outgoing Bot API call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(5000)

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=next(self._ids),
                date=int(time.time()),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=1, is_bot=True, first_name="PhoneIntel"),
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup,
            ).as_(bot)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    # helpers -----------------------------------------------------------
    def texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText)) and c.text]

    def last_text(self) -> str:
        return self.texts()[-1]

    def button(self, label: str) -> str:
        for call in reversed(self.calls):
            markup = getattr(call, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", None) or []:
                for button in row:
                    if label.lower() in button.text.lower():
                        return str(button.callback_data)
        raise AssertionError(f"no button containing {label!r}")

    def documents(self) -> list[SendDocument]:
        return [c for c in self.calls if isinstance(c, SendDocument)]


class Harness:
    def __init__(self, backend: LiveBackend, service_key: str, **overrides: Any) -> None:
        settings = BotSettings(
            bot_token="123456:TEST-TOKEN",
            service_api_key=service_key,
            api_url=backend.base_url,
            poll_interval_seconds=0.05,
            discovery_timeout_seconds=60,
            _env_file=None,
            **overrides,
        )
        self.session = RecordingSession()
        self.bot = Bot("123456:TEST-TOKEN", session=self.session)
        self.backend_client = BackendClient(settings)
        self.dp, self.tracker = build_dispatcher(
            settings, self.backend_client, limiter=MemoryRateLimiter(settings.rate_limit_per_minute)
        )
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)

    async def send(self, user_id: int, text: str, chat_type: str = "private") -> None:
        chat_id = user_id if chat_type == "private" else -100123
        update = Update.model_validate(
            {
                "update_id": next(self._update_ids),
                "message": {
                    "message_id": next(self._message_ids),
                    "date": int(time.time()),
                    "chat": {"id": chat_id, "type": chat_type},
                    "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                    "text": text,
                },
            },
            context={"bot": self.bot},
        )
        await self.dp.feed_update(self.bot, update)

    async def press(self, user_id: int, callback_data: str) -> None:
        update = Update.model_validate(
            {
                "update_id": next(self._update_ids),
                "callback_query": {
                    "id": f"cb{next(self._update_ids)}",
                    "chat_instance": "ci",
                    "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                    "message": {
                        "message_id": 1,
                        "date": int(time.time()),
                        "chat": {"id": user_id, "type": "private"},
                        "from": {"id": 1, "is_bot": True, "first_name": "PhoneIntel"},
                        "text": "x",
                    },
                    "data": callback_data,
                },
            },
            context={"bot": self.bot},
        )
        await self.dp.feed_update(self.bot, update)

    async def close(self) -> None:
        await self.tracker.shutdown()
        await self.backend_client.aclose()


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveBackend]:
    server = LiveBackend(tmp_path_factory.mktemp("bot-backend")).start()
    yield server
    server.stop()


@pytest.fixture(scope="module")
def service_key(backend: LiveBackend) -> str:
    return backend.create_service_key()


def link_code(backend: LiveBackend, email: str) -> str:
    with httpx.Client(base_url=backend.base_url) as http:
        http.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD}).raise_for_status()
        token = http.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
        r = http.post("/api/v1/integrations/telegram/link-code", headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return str(r.json()["code"])


@pytest.fixture
async def harness(backend: LiveBackend, service_key: str) -> AsyncGenerator[Harness, None]:
    h = Harness(backend, service_key)
    yield h
    await h.close()


def test_split_message_respects_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, max_len=500)
    assert all(len(c) <= 500 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")
    assert split_message("y" * 1200, max_len=500) == ["y" * 500, "y" * 500, "y" * 200]


async def test_static_commands(harness: Harness) -> None:
    for command, expected in (("/start", "link"), ("/help", "/profile_links"), ("/privacy", "never bypassed")):
        await harness.send(42, command)
        assert expected in harness.session.last_text()
    await harness.send(42, "/nonsense")
    assert "Unknown command" in harness.session.last_text()


async def test_group_chats_are_refused(harness: Harness) -> None:
    await harness.send(42, "/validate +919876543210", chat_type="supergroup")
    assert "private chat" in harness.session.last_text()
    assert len(harness.session.calls) == 1


async def test_unlinked_user_is_told_to_link(harness: Harness) -> None:
    await harness.send(777001, "/validate +919876543210")
    assert "/link CODE" in harness.session.last_text()


async def test_full_linked_flow(harness: Harness, backend: LiveBackend) -> None:
    uid = 900001
    await harness.send(uid, "/link " + link_code(backend, "tg-analyst@example.org"))
    assert "Linked to PHONEINTEL account" in harness.session.last_text()
    assert any(isinstance(c, DeleteMessage) for c in harness.session.calls)  # code removed from chat

    await harness.send(uid, "/validate +91 98765 43210")
    assert "+919876543210" in harness.session.last_text() and "VALID" in harness.session.last_text()
    await harness.send(uid, "/country +919876543210")
    assert "India" in harness.session.last_text()
    await harness.send(uid, "/carrier +919876543210")
    assert "Carrier source" in harness.session.last_text()
    await harness.send(uid, "/validate +12345")
    assert "INVALID" in harness.session.last_text() or "POSSIBLE" in harness.session.last_text()

    # Discovery: basis -> purpose -> explicit confirmation, with HTML escaping of user input.
    await harness.send(uid, "/profile-links +919876543210")
    assert "Step 1/3" in harness.session.last_text()
    await harness.press(uid, harness.session.button("Fraud prevention"))
    assert "Step 2/3" in harness.session.last_text()
    await harness.send(uid, "short")
    assert "between 10 and 1000" in harness.session.last_text()
    await harness.send(uid, "Ticket <b>4411</b> fraud triage")
    confirm = harness.session.last_text()
    assert "Step 3/3" in confirm and "&lt;b&gt;4411&lt;/b&gt;" in confirm and "I confirm" in confirm
    await harness.press(uid, harness.session.button("I confirm"))
    await harness.tracker.wait_idle()
    results = "\n".join(harness.session.texts()[-3:])
    assert "https://t.me/acmehelp" in results
    assert "VERIFIED_PUBLIC_REFERENCE" in results
    assert "acme_support" in results

    await harness.send(uid, "/history")
    history = harness.session.last_text()
    assert "PI-" in history and "completed" in history
    reference = history.split("<code>")[1].split("</code>")[0]

    await harness.send(uid, "/report")
    pdf = harness.session.documents()[-1]
    assert pdf.document.data.startswith(b"%PDF")  # type: ignore[union-attr]
    assert pdf.protect_content is True
    await harness.send(uid, f"/report {reference} json")
    assert harness.session.documents()[-1].document.filename.endswith(".json")  # type: ignore[union-attr]
    await harness.press(uid, harness.session.button("CSV"))
    assert harness.session.documents()[-1].document.filename.endswith(".csv")  # type: ignore[union-attr]

    # Another Telegram user, linked to another account, cannot see any of it.
    other = 900002
    await harness.send(other, "/link " + link_code(backend, "tg-other@example.org"))
    await harness.send(other, "/history")
    assert "no investigations" in harness.session.last_text()
    await harness.send(other, f"/report {reference}")
    assert "No matching investigation" in harness.session.last_text()

    # Erasure with explicit confirmation.
    await harness.send(uid, "/delete_data")
    assert "cannot be undone" in harness.session.last_text()
    await harness.press(uid, harness.session.button("Yes, erase"))
    assert "Erased 1 investigation" in harness.session.last_text()
    await harness.send(uid, "/history")
    assert "no investigations" in harness.session.last_text()

    await harness.send(uid, "/unlink")
    assert "Unlinked" in harness.session.last_text()
    await harness.send(uid, "/history")
    assert "/link CODE" in harness.session.last_text()


async def test_cancel_dialog_and_search_public(harness: Harness, backend: LiveBackend) -> None:
    uid = 900003
    await harness.send(uid, "/link " + link_code(backend, "tg-cancel@example.org"))
    await harness.send(uid, "/search_public +919876543210")
    assert "Step 1/3" in harness.session.last_text()
    await harness.send(uid, "/cancel")
    assert "Nothing was created" in harness.session.last_text()
    await harness.send(uid, "/cancel")
    assert "Nothing to cancel" in harness.session.last_text()
    await harness.send(uid, "/history")
    assert "no investigations" in harness.session.last_text()


async def test_rate_limit_and_allowlist(backend: LiveBackend, service_key: str) -> None:
    limited = Harness(backend, service_key, rate_limit_per_minute=3)
    try:
        for _ in range(4):
            await limited.send(55, "/help")
        assert "Too many requests" in limited.session.last_text()
    finally:
        await limited.close()
    restricted = Harness(backend, service_key, allowed_user_ids="1,2,3")
    try:
        await restricted.send(55, "/help")
        assert "not authorised" in restricted.session.last_text()
        await restricted.send(2, "/help")
        assert "/profile_links" in restricted.session.last_text()
    finally:
        await restricted.close()
