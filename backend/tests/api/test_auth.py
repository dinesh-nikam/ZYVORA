from __future__ import annotations

import httpx
import pytest

from app.services.container import Container
from tests.conftest import make_settings, register_and_login


async def test_register_login_me(client: httpx.AsyncClient) -> None:
    user = await register_and_login(client, "Alice@Example.org")
    r = await client.get("/api/v1/auth/me", headers=user.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "alice@example.org"
    assert body["role"] == "analyst"
    assert "investigation:create" in body["permissions"]
    assert "admin:users" not in body["permissions"]


@pytest.mark.parametrize("payload", [
    {"email": "not-an-email", "password": "Correct-Horse-Battery-9"},
    {"email": "a@example.org", "password": "short"},
    {"email": "a@example.org"},
    {"email": "a@example.org", "password": "Correct-Horse-Battery-9", "role": "admin"},  # mass assignment
])
async def test_register_rejects_invalid_payloads(client: httpx.AsyncClient, payload: dict) -> None:
    r = await client.post("/api/v1/auth/register", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_duplicate_registration(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "dup@example.org")
    r = await client.post("/api/v1/auth/register", json={"email": "dup@example.org",
                                                          "password": "Correct-Horse-Battery-9"})
    assert r.status_code == 409


async def test_login_failure_is_generic(client: httpx.AsyncClient) -> None:
    await register_and_login(client, "bob@example.org")
    wrong = await client.post("/api/v1/auth/login", json={"email": "bob@example.org", "password": "Wrong-Password-1"})
    unknown = await client.post("/api/v1/auth/login", json={"email": "nobody@example.org", "password": "Wrong-P-1x"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_account_lockout(tmp_path, web) -> None:
    from app.main import create_app
    from app.services.container import build_container
    from tests.conftest import _prepare_db

    settings = make_settings(tmp_path, max_failed_logins=3)
    container = await build_container(settings, resolver=web.resolve, transport=web.transport(), eager_jobs=True)
    await _prepare_db(container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container)),
                                 base_url="http://t") as client:
        user = await register_and_login(client, "lock@example.org")
        for _ in range(3):
            await client.post("/api/v1/auth/login", json={"email": user.email, "password": "Wrong-Password-1"})
        r = await client.post("/api/v1/auth/login", json={"email": user.email, "password": user.password})
        assert r.status_code == 401
    await container.aclose()


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    user = await register_and_login(client)
    r1 = await client.post("/api/v1/auth/refresh", json={"refresh_token": user.refresh})
    assert r1.status_code == 200
    rotated = r1.json()["refresh_token"]
    assert rotated != user.refresh
    # Re-using the old token is detected and revokes the whole family, including the rotated token.
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": user.refresh})
    assert reuse.status_code == 401
    assert reuse.json()["error"]["code"] == "refresh_token_reused"
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    assert after.status_code == 401


async def test_logout_revokes_access_and_refresh(client: httpx.AsyncClient) -> None:
    user = await register_and_login(client)
    r = await client.post("/api/v1/auth/logout", headers=user.headers, json={"refresh_token": user.refresh})
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=user.headers)).status_code == 401
    assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": user.refresh})).status_code == 401


async def test_api_key_lifecycle(client: httpx.AsyncClient) -> None:
    user = await register_and_login(client)
    r = await client.post("/api/v1/api-keys", headers=user.headers,
                          json={"name": "cli laptop", "permissions": ["phone:validate", "investigation:read"]})
    assert r.status_code == 201
    created = r.json()
    key = created["api_key"]
    assert key.startswith("pi_")
    listed = (await client.get("/api/v1/api-keys", headers=user.headers)).json()
    assert all("api_key" not in item for item in listed)  # plaintext is never returned again
    ok = await client.post("/api/v1/phone/validate", headers={"X-API-Key": key}, json={"phone": "+919876543210"})
    assert ok.status_code == 200
    # Scoped key cannot create investigations.
    denied = await client.post("/api/v1/investigations", headers={"X-API-Key": key}, json={
        "phone": "+919876543210", "purpose": "scoped key check", "authorization_basis": "own_number",
        "acknowledge_limitations": True})
    assert denied.status_code == 403
    assert (await client.delete(f"/api/v1/api-keys/{created['id']}", headers=user.headers)).status_code == 204
    revoked = await client.post("/api/v1/phone/validate", headers={"X-API-Key": key}, json={"phone": "+91"})
    assert revoked.status_code == 401


async def test_api_key_cannot_escalate(client: httpx.AsyncClient) -> None:
    user = await register_and_login(client)
    r = await client.post("/api/v1/api-keys", headers=user.headers,
                          json={"name": "escalate", "permissions": ["admin:users"]})
    assert r.status_code == 403


async def test_pending_accounts_cannot_login(tmp_path, web) -> None:
    from app.main import create_app
    from app.services.container import build_container
    from tests.conftest import _prepare_db

    settings = make_settings(tmp_path, default_user_status="pending")
    container = await build_container(settings, resolver=web.resolve, transport=web.transport(), eager_jobs=True)
    await _prepare_db(container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container)),
                                 base_url="http://t") as client:
        r = await client.post("/api/v1/auth/register", json={"email": "p@example.org",
                                                              "password": "Correct-Horse-Battery-9"})
        assert r.json()["status"] == "pending"
        login = await client.post("/api/v1/auth/login", json={"email": "p@example.org",
                                                               "password": "Correct-Horse-Battery-9"})
        assert login.status_code == 403
        assert login.json()["error"]["code"] == "account_pending"
    await container.aclose()


async def test_auth_rate_limit(tmp_path, web) -> None:
    from app.main import create_app
    from app.services.container import build_container
    from tests.conftest import _prepare_db

    settings = make_settings(tmp_path, rate_limit_auth="3/minute")
    container: Container = await build_container(settings, resolver=web.resolve, transport=web.transport())
    await _prepare_db(container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, container)),
                                 base_url="http://t") as client:
        codes = [(await client.post("/api/v1/auth/login", json={"email": "x@example.org", "password": "Wrong-1234567"}
                                    )).status_code for _ in range(5)]
        assert codes[:3] == [401, 401, 401]
        assert codes[3] == 429
        limited = await client.post("/api/v1/auth/login", json={"email": "x@example.org", "password": "x"})
        assert "retry-after" in limited.headers
    await container.aclose()
