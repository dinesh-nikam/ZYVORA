from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text

from app.services.container import Container
from tests.conftest import PHONE, UserCtx, create_investigation, register_and_login


async def test_phone_validate_and_metadata(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    r = await client.post("/api/v1/phone/validate", headers=analyst.headers, json={"phone": "+91 98765 43210"})
    assert r.status_code == 200
    body = r.json()
    assert body["e164"] == PHONE
    assert body["country_code"] == 91
    assert body["region_code"] == "IN"
    assert body["validation_status"] == "VALID"
    meta = (await client.post("/api/v1/phone/metadata", headers=analyst.headers, json={"phone": PHONE})).json()
    assert meta["carrier_source"].startswith("libphonenumber")
    assert meta["timezones"] == ["Asia/Calcutta"] or meta["timezones"]


@pytest.mark.parametrize("payload", [{"phone": "abc"}, {"phone": ""}, {"phone": "+1"}, {}, {"phone": 12345},
                                     {"phone": "+919876543210", "region": "INDIA"}])
async def test_phone_validate_invalid_payloads(client: httpx.AsyncClient, analyst: UserCtx, payload: dict) -> None:
    r = await client.post("/api/v1/phone/validate", headers=analyst.headers, json=payload)
    assert r.status_code == 422


async def test_requires_authentication(client: httpx.AsyncClient) -> None:
    for method, url in [("GET", "/api/v1/investigations"), ("POST", "/api/v1/phone/validate"),
                        ("GET", "/api/v1/admin/users"), ("GET", f"/api/v1/profiles/{uuid.uuid4()}")]:
        r = await client.request(method, url, json={})
        assert r.status_code == 401, url
        assert r.json()["error"]["code"] == "authentication_failed"


async def test_create_requires_basis_purpose_ack(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    base = {"phone": PHONE, "purpose": "Valid purpose text", "authorization_basis": "fraud_prevention",
            "acknowledge_limitations": True}
    for broken in ({**base, "authorization_basis": "curiosity"}, {**base, "purpose": "short"},
                   {k: v for k, v in base.items() if k != "authorization_basis"},
                   {**base, "acknowledge_limitations": False}, {**base, "phone": "+12345"}):
        r = await client.post("/api/v1/investigations", headers=analyst.headers, json=broken)
        assert r.status_code == 422, broken


async def test_investigation_crud_with_encryption_at_rest(client: httpx.AsyncClient, analyst: UserCtx,
                                                           container: Container) -> None:
    inv = await create_investigation(client, analyst)
    assert inv["normalized_phone"] == PHONE
    assert inv["investigation_reference"].startswith("PI-")
    assert inv["phone_intelligence"]["country_code"] == 91
    async with container.engine.connect() as conn:
        raw = (await conn.execute(text("SELECT normalized_phone, purpose FROM investigations"))).one()
    assert PHONE not in raw[0] and raw[0].startswith("v1.")
    assert "Fraud report" not in raw[1]

    got = await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)
    assert got.status_code == 200
    assert got.json()["investigation"]["purpose"].startswith("Fraud report")

    listing = (await client.get("/api/v1/investigations", headers=analyst.headers, params={"phone": PHONE})).json()
    assert listing["total"] == 1

    assert (await client.delete(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).status_code == 204
    assert (await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).status_code == 404
    async with container.engine.connect() as conn:
        row = (await conn.execute(text("SELECT normalized_phone, purpose, deleted_at FROM investigations"))).one()
    assert row[0] is None and row[1] is None and row[2] is not None


async def test_pagination_filtering_sorting(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    for _ in range(5):
        await create_investigation(client, analyst)
    await create_investigation(client, analyst, phone="+14155552671")
    page = (await client.get("/api/v1/investigations", headers=analyst.headers,
                             params={"page": 2, "page_size": 4, "sort": "created_at"})).json()
    assert page["total"] == 6 and len(page["items"]) == 2 and page["page"] == 2
    filtered = (await client.get("/api/v1/investigations", headers=analyst.headers,
                                 params={"phone": "+1 415 555 2671"})).json()
    assert filtered["total"] == 1
    status = (await client.get("/api/v1/investigations", headers=analyst.headers,
                               params={"status": "created"})).json()
    assert status["total"] == 6
    bad_sort = await client.get("/api/v1/investigations", headers=analyst.headers, params={"sort": "password_hash"})
    assert bad_sort.status_code == 422
    assert (await client.get("/api/v1/investigations", headers=analyst.headers,
                             params={"page_size": 1000})).status_code == 422


async def test_cross_user_access_is_denied(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    inv = await create_investigation(client, analyst)
    mallory = await register_and_login(client, "mallory@example.org")
    iid = inv["id"]
    for method, url in [
        ("GET", f"/api/v1/investigations/{iid}"),
        ("DELETE", f"/api/v1/investigations/{iid}"),
        ("GET", f"/api/v1/investigations/{iid}/profiles"),
        ("GET", f"/api/v1/investigations/{iid}/report"),
        ("GET", f"/api/v1/investigations/{iid}/report/export?format=json"),
        ("POST", f"/api/v1/investigations/{iid}/cancel"),
    ]:
        r = await client.request(method, url, headers=mallory.headers)
        assert r.status_code == 404, (method, url, r.status_code)
    r = await client.post(f"/api/v1/investigations/{iid}/profile-discovery", headers=mallory.headers, json={})
    assert r.status_code == 404
    assert (await client.get("/api/v1/investigations", headers=mallory.headers)).json()["total"] == 0
    # The owner still has access.
    assert (await client.get(f"/api/v1/investigations/{iid}", headers=analyst.headers)).status_code == 200


async def test_admin_can_read_others_and_it_is_audited(client: httpx.AsyncClient, analyst: UserCtx,
                                                       container: Container) -> None:
    from tests.conftest import promote

    inv = await create_investigation(client, analyst)
    admin = await register_and_login(client, "root@example.org")
    await promote(container, admin.email, "admin")
    r = await client.get(f"/api/v1/investigations/{inv['id']}", headers=admin.headers)
    assert r.status_code == 200
    logs = (await client.get("/api/v1/admin/audit-logs", headers=admin.headers,
                             params={"action": "investigation.read_other_user"})).json()
    assert logs["total"] == 1
    # Admins still cannot delete someone else's investigation.
    assert (await client.delete(f"/api/v1/investigations/{inv['id']}", headers=admin.headers)).status_code == 404
