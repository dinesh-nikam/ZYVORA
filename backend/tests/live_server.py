"""Runs the real PHONEINTEL API in a background thread (uvicorn) so that the CLI
and the Telegram bot can be tested end-to-end over HTTP against the same
backend the REST tests use.

Public internet access is still replaced by :class:`tests.conftest.MockWeb`.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import uvicorn
from app.core.permissions import BUILTIN_ROLES
from app.main import create_app
from app.services.audit import AuditContext
from app.services.auth import AuthService, Principal
from app.services.container import Container, build_container

from tests.conftest import MockWeb, _prepare_db, default_web, make_settings

T = TypeVar("T")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveBackend:
    def __init__(self, tmp_path: Path, web: MockWeb | None = None, **overrides: Any) -> None:
        self.web = web or default_web()
        overrides.setdefault("job_backoff_base_seconds", 0.01)
        self.settings = make_settings(tmp_path, **overrides)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.container: Container | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> LiveBackend:
        self._thread = threading.Thread(target=self._run, name="live-backend", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._error is not None:
                raise RuntimeError("live backend failed to start") from self._error
            if self._server is not None and self._server.started:
                return self
            time.sleep(0.05)
        raise TimeoutError("live backend did not start within 60s")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            self.container = loop.run_until_complete(
                build_container(self.settings, resolver=self.web.resolve, transport=self.web.transport())
            )
            loop.run_until_complete(_prepare_db(self.container))
            app = create_app(self.settings, self.container)
            config = uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off", ws="none"
            )
            self._server = uvicorn.Server(config)
            loop.run_until_complete(self._server.serve())
        except BaseException as exc:  # surfaced to the test thread by start()
            self._error = exc
        finally:
            if self.container is not None:
                loop.run_until_complete(self.container.aclose())
            loop.close()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=30)

    # -------------------------------------------------------------- helpers
    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 30) -> T:
        """Execute a coroutine on the server's event loop (e.g. to seed data)."""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def create_service_key(self, name: str = "telegram-bot") -> str:
        async def _create() -> str:
            assert self.container is not None
            c = self.container
            async with c.sessionmaker() as session:
                auth = AuthService(session, c.settings, c.tokens, c.redis, c.indexer)
                user = await auth.register(
                    f"svc-{name}@service.phoneintel.invalid",
                    "Svc-Account-Password-1",
                    AuditContext(None, None, "system"),
                    role_name="service_telegram",
                    status="active",
                    bypass_closed=True,
                )
                principal = Principal(user=user, permissions=BUILTIN_ROLES["service_telegram"], via="system")
                _, key = await auth.create_api_key(principal, name, None, 30, AuditContext(user.id, None))
                await session.commit()
                return key

        return self.run(_create())

    def promote(self, email: str, role: str) -> None:
        from tests.conftest import promote

        assert self.container is not None
        self.run(promote(self.container, email, role))
