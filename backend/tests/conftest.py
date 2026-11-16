"""Shared fixtures.

The suite runs the real application stack (FastAPI app, services, SQLAlchemy,
verification engine, search provider clients, job runner) against:

* SQLite (aiosqlite) - PostgreSQL-specific behaviour is covered by
  ``tests/integration/test_postgres.py`` when ``TEST_DATABASE_URL`` is set;
* fakeredis (``REDIS_URL=memory://``);
* ``httpx.MockTransport`` standing in for the public internet, plus a fake DNS
  resolver, so no test depends on third-party availability.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from app.core.config import Settings
from app.core.permissions import BUILTIN_ROLES
from app.db.base import Base
from app.main import create_app
from app.models import Role
from app.services.container import Container, build_container
from sqlalchemy import select

PHONE = "+919876543210"
PUBLIC_IP = "93.184.216.34"


def make_settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        "redis_url": "memory://",
        "job_backend": "inline",
        "secret_key": "test-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz",
        "blind_index_key": "test-blind-index-key-0123456789-abcdefghijkl",
        "field_encryption_keys": "k1:" + base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "rate_limit_default": "10000/minute",
        "rate_limit_auth": "10000/minute",
        "rate_limit_discovery": "10000/hour",
        "rate_limit_phone": "10000/minute",
        "rate_limit_verify": "10000/hour",
        "brave_api_key": "test-brave-key",
        "search_providers": "brave",
        "log_json": False,
        "log_level": "WARNING",
        "job_backoff_base_seconds": 0.01,
        "password_min_length": 12,
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class MockWeb:
    """Programmable stand-in for the public internet."""

    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    resolver_map: dict[str, list[str]] = field(default_factory=dict)

    def add(self, url: str, handler: Callable[[httpx.Request], httpx.Response] | httpx.Response) -> None:
        parsed = httpx.URL(url)
        fn = handler if callable(handler) else (lambda _r, resp=handler: resp)
        self.routes[(parsed.host, parsed.path)] = fn

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fn = self.routes.get((request.url.host, request.url.path))
        if fn is None:
            return httpx.Response(404, text="not found")
        return fn(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    async def resolve(self, host: str, port: int) -> list[str]:
        if host in self.resolver_map:
            return self.resolver_map[host]
        if host.endswith(".invalid") or host.startswith("nxdomain"):
            raise OSError("NXDOMAIN")
        return [PUBLIC_IP]

    def hosts_called(self) -> list[str]:
        return [r.url.host for r in self.requests]


def brave_payload(results: list[dict[str, str]]) -> httpx.Response:
    return httpx.Response(200, json={"web": {"results": results}})


TELEGRAM_PAGE = """<!DOCTYPE html><html><head><title>Telegram: Contact @acmehelp</title>
<meta property="og:title" content="ACME Help Desk"><meta property="og:description" content="Support line +91 98765 43210, write to help@acme.example">
<link rel="canonical" href="https://t.me/acmehelp"></head><body>
<div class="tgme_page_title"><span dir="auto">ACME Help Desk</span></div>
<div class="tgme_page_description">Official support. Call +91 98765 43210 or 022 2345 6789.</div></body></html>"""


def default_web() -> MockWeb:
    web = MockWeb()
    results = [
        {"url": "https://www.instagram.com/acme_support/", "title": "ACME Support (@acme_support)",
         "description": "Customer care: +91 98765 43210. DM us for help."},
        {"url": "https://example-directory.com/listing/42", "title": "ACME listing",
         "description": "Phone <strong>+919876543210</strong> - Telegram t.me/acmehelp - github.com/acmedev"},
        {"url": "https://www.facebook.com/john.doe.123", "title": "John Doe | Facebook",
         "description": "John Doe is on Facebook. Join Facebook to connect with John Doe."},
        {"url": "https://www.instagram.com/acme_support", "title": "duplicate", "description": "dup"},
        {"url": "https://unrelated.example.org/page", "title": "Numbers",
         "description": "Our office number is +91 98765 43210."},
    ]
    web.add("https://api.search.brave.com/res/v1/web/search", brave_payload(results))
    web.add("https://t.me/robots.txt", httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    web.add("https://t.me/acmehelp", httpx.Response(200, text=TELEGRAM_PAGE,
                                                    headers={"content-type": "text/html; charset=utf-8"}))
    web.add("https://api.github.com/users/acmedev", httpx.Response(200, json={
        "login": "acmedev", "name": "ACME Dev", "html_url": "https://github.com/acmedev",
        "bio": "Hotline +91-98765-43210", "blog": "", "company": "ACME"}))
    web.add("https://api.github.com/users/ghost-user", httpx.Response(404, json={"message": "Not Found"}))
    return web


@pytest.fixture
def web() -> MockWeb:
    return default_web()


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return make_settings(tmp_path)


async def _prepare_db(container: Container) -> None:
    async with container.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with container.sessionmaker() as session:
        existing = {r.name for r in (await session.execute(select(Role))).scalars()}
        for name, perms in BUILTIN_ROLES.items():
            if name not in existing:
                session.add(Role(name=name, permissions=sorted(p.value for p in perms)))
        await session.commit()


@pytest.fixture
async def container(settings: Settings, web: MockWeb) -> AsyncIterator[Container]:
    c = await build_container(settings, resolver=web.resolve, transport=web.transport(), eager_jobs=True)
    await _prepare_db(c)
    yield c
    await c.aclose()


@pytest.fixture
async def client(container: Container, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@dataclass
class UserCtx:
    email: str
    password: str
    access: str
    refresh: str
    id: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access}"}


async def register_and_login(client: httpx.AsyncClient, email: str | None = None,
                             password: str = "Correct-Horse-Battery-9") -> UserCtx:
    email = email or f"analyst-{uuid.uuid4().hex[:8]}@example.org"
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    return UserCtx(email, password, body["access_token"], body["refresh_token"], user_id)


async def promote(container: Container, email: str, role: str) -> None:
    from app.models import User

    async with container.sessionmaker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.role_id = (await session.execute(select(Role).where(Role.name == role))).scalar_one().id
        await session.commit()


@pytest.fixture
async def analyst(client: httpx.AsyncClient) -> UserCtx:
    return await register_and_login(client)


async def create_investigation(client: httpx.AsyncClient, user: UserCtx, phone: str = PHONE) -> dict[str, Any]:
    r = await client.post("/api/v1/investigations", headers=user.headers, json={
        "phone": phone, "purpose": "Fraud report triage for ticket 4411", "authorization_basis": "fraud_prevention",
        "authorization_reference": "TICKET-4411", "acknowledge_limitations": True,
    })
    assert r.status_code == 201, r.text
    return r.json()


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)
