from __future__ import annotations

import httpx
from app.main import create_app
from app.services.container import Container
from fastapi.testclient import TestClient

from tests.conftest import UserCtx, create_investigation, promote, register_and_login


async def test_admin_endpoints_require_role(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    for url in ("/api/v1/admin/users", "/api/v1/admin/audit-logs", "/api/v1/admin/system-health"):
        r = await client.get(url, headers=analyst.headers)
        assert r.status_code == 403, url
        assert r.json()["error"]["code"] == "permission_denied"


async def test_admin_user_management(client: httpx.AsyncClient, container: Container, analyst: UserCtx) -> None:
    admin = await register_and_login(client, "admin@example.org")
    await promote(container, admin.email, "admin")
    users = (await client.get("/api/v1/admin/users", headers=admin.headers, params={"role": "analyst"})).json()
    assert users["total"] == 1
    assert "password_hash" not in users["items"][0]
    target = users["items"][0]["id"]
    r = await client.patch(f"/api/v1/admin/users/{target}", headers=admin.headers, json={"status": "suspended"})
    assert r.status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=analyst.headers)).status_code == 401
    assert (await client.patch(f"/api/v1/admin/users/{admin.id}", headers=admin.headers,
                               json={"role": "analyst"})).status_code == 422
    health = (await client.get("/api/v1/admin/system-health", headers=admin.headers)).json()
    assert health["components"]["database"]["status"] == "ok"
    assert health["components"]["redis"]["status"] == "ok"
    assert health["search_providers"] == ["brave"]


async def test_auditor_reads_logs_but_not_investigations(client: httpx.AsyncClient, container: Container,
                                                         analyst: UserCtx) -> None:
    await create_investigation(client, analyst)
    auditor = await register_and_login(client, "auditor@example.org")
    await promote(container, auditor.email, "auditor")
    logs = (await client.get("/api/v1/admin/audit-logs", headers=auditor.headers,
                             params={"action": "investigation.create"})).json()
    assert logs["total"] == 1
    entry = logs["items"][0]
    assert "+919876543210" not in str(entry) and "9876543210" not in str(entry)
    assert (await client.get("/api/v1/investigations", headers=auditor.headers)).status_code == 403


async def test_data_export_and_erasure(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    inv = await create_investigation(client, analyst)
    await client.post(f"/api/v1/investigations/{inv['id']}/profile-discovery", headers=analyst.headers, json={})
    export = (await client.get("/api/v1/me/export", headers=analyst.headers)).json()
    assert export["account"]["email"] == analyst.email
    assert len(export["investigations"]) == 1
    erased = (await client.delete("/api/v1/me/data", headers=analyst.headers,
                                  params={"include_account": True})).json()
    assert erased == {"investigations_erased": 1, "account_closed": True}
    assert (await client.get("/api/v1/auth/me", headers=analyst.headers)).status_code == 401
    relogin = await client.post("/api/v1/auth/login", json={"email": analyst.email, "password": analyst.password})
    assert relogin.status_code == 401


async def test_telegram_link_and_delegation(client: httpx.AsyncClient, container: Container,
                                            analyst: UserCtx) -> None:
    from app.core.permissions import BUILTIN_ROLES
    from app.services.audit import AuditContext
    from app.services.auth import AuthService, Principal

    async with container.sessionmaker() as session:
        auth = AuthService(session, container.settings, container.tokens, container.redis, container.indexer)
        svc_user = await auth.register("svc-bot@service.phoneintel.invalid", "Svc-Account-Password-1",
                                       AuditContext(None, None, "system"), role_name="service_telegram")
        principal = Principal(user=svc_user, permissions=BUILTIN_ROLES["service_telegram"], via="system")
        _, svc_key = await auth.create_api_key(principal, "bot", None, 30, AuditContext(svc_user.id, None))
        await session.commit()
    svc = {"X-API-Key": svc_key}

    # Service key alone cannot use user endpoints.
    assert (await client.get("/api/v1/investigations", headers=svc)).status_code == 401
    # Unlinked Telegram user.
    r = await client.get("/api/v1/investigations", headers={**svc, "X-Telegram-User-Id": "4242"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "telegram_not_linked"
    # A normal user cannot use the delegation header.
    spoof = await client.get("/api/v1/investigations", headers={**analyst.headers, "X-Telegram-User-Id": "4242"})
    assert spoof.status_code == 401

    code = (await client.post("/api/v1/integrations/telegram/link-code", headers=analyst.headers)).json()["code"]
    bad = await client.post("/api/v1/integrations/telegram/link", headers=svc,
                            json={"code": "WRONGCODE1", "telegram_user_id": 4242})
    assert bad.status_code == 422
    ok = await client.post("/api/v1/integrations/telegram/link", headers=svc,
                           json={"code": code, "telegram_user_id": 4242})
    assert ok.status_code == 200 and ok.json()["email"] == analyst.email
    reuse = await client.post("/api/v1/integrations/telegram/link", headers=svc,
                              json={"code": code, "telegram_user_id": 4242})
    assert reuse.status_code == 422  # one-time code

    tg = {**svc, "X-Telegram-User-Id": "4242"}
    inv = None
    created = await client.post("/api/v1/investigations", headers=tg, json={
        "phone": "+919876543210", "purpose": "Telegram initiated check", "authorization_basis": "own_number",
        "acknowledge_limitations": True})
    assert created.status_code == 201
    mine = (await client.get("/api/v1/investigations", headers=analyst.headers)).json()
    assert mine["total"] == 1  # same user via Telegram and via API
    # Another Telegram user sees nothing.
    other = await client.get("/api/v1/investigations", headers={**svc, "X-Telegram-User-Id": "5555"})
    assert other.status_code == 403
    # Delegated principal cannot mint API keys.
    assert (await client.post("/api/v1/api-keys", headers=tg, json={"name": "x"})).status_code == 403
    unlink = await client.post("/api/v1/integrations/telegram/unlink", headers=svc, json={"telegram_user_id": 4242})
    assert unlink.json()["message"] == "unlinked"
    assert (await client.get("/api/v1/investigations", headers=tg)).status_code == 403
    assert inv is None


def test_websocket_events(settings, container: Container) -> None:
    app = create_app(settings, container)
    with TestClient(app) as tc:
        r = tc.post("/api/v1/auth/register", json={"email": "ws@example.org", "password": "Correct-Horse-Battery-9"})
        assert r.status_code == 201
        token = tc.post("/api/v1/auth/login", json={"email": "ws@example.org",
                                                     "password": "Correct-Horse-Battery-9"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        inv = tc.post("/api/v1/investigations", headers=headers, json={
            "phone": "+919876543210", "purpose": "Websocket stream test", "authorization_basis": "own_number",
            "acknowledge_limitations": True}).json()
        tc.post(f"/api/v1/investigations/{inv['id']}/profile-discovery", headers=headers,
                json={"platforms": ["telegram"]})

        with tc.websocket_connect(f"/api/v1/ws/investigations/{inv['id']}") as ws:
            ws.send_json({"type": "auth", "token": token})
            assert ws.receive_json()["type"] == "auth.ok"
            seen = []
            while True:
                event = ws.receive_json()
                seen.append(event["type"])
                if event["type"] == "investigation.completed":
                    break
        assert seen[0] == "investigation.created"
        assert "profile.verified" in seen

        with tc.websocket_connect(f"/api/v1/ws/investigations/{inv['id']}") as ws:
            ws.send_json({"type": "auth", "token": "garbage"})
            closed = ws.receive()
            assert closed["type"] == "websocket.close" and closed["code"] == 4401
