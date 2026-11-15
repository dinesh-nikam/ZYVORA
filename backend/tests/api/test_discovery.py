from __future__ import annotations

import uuid

import httpx

from app.models.enums import JobStatus
from app.services.container import Container
from tests.conftest import UserCtx, create_investigation


async def run_discovery(client: httpx.AsyncClient, user: UserCtx, inv_id: str, **body) -> httpx.Response:
    return await client.post(f"/api/v1/investigations/{inv_id}/profile-discovery", headers=user.headers,
                             json={"platforms": ["telegram", "instagram", "facebook", "github", "whatsapp"], **body})


async def test_full_discovery_pipeline(client: httpx.AsyncClient, analyst: UserCtx, container: Container) -> None:
    inv = await create_investigation(client, analyst)
    r = await run_discovery(client, analyst, inv["id"], profile_urls=["https://github.com/ghost-user"])
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"

    detail = (await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).json()
    assert detail["investigation"]["status"] == "completed"
    assert detail["latest_job"]["status"] == JobStatus.SUCCEEDED.value
    summary = detail["investigation"]["summary"]
    assert summary["search"]["providers"] == ["brave"]
    assert summary["counts"]["total_profiles"] >= 5

    profiles = (await client.get(f"/api/v1/investigations/{inv['id']}/profiles", headers=analyst.headers)).json()
    by_url = {p["canonical_url"]: p for p in profiles["items"]}
    assert by_url["https://t.me/acmehelp"]["verification_status"] == "VERIFIED_PUBLIC_REFERENCE"
    assert by_url["https://t.me/acmehelp"]["confidence_level"] == "HIGH"
    assert by_url["https://github.com/acmedev"]["verification_status"] == "VERIFIED_PUBLIC_REFERENCE"
    assert by_url["https://www.instagram.com/acme_support/"]["verification_status"] == "PUBLIC_CANDIDATE"
    assert by_url["https://www.facebook.com/john.doe.123"]["verification_status"] == "UNVERIFIED"
    assert by_url["https://www.facebook.com/john.doe.123"]["confidence_level"] in ("NONE", "LOW")
    assert by_url["https://wa.me/919876543210"]["discovery_source"] == "generated_official_link"
    assert by_url["https://wa.me/919876543210"]["confidence_score"] == 0
    assert by_url["https://github.com/ghost-user"]["verification_status"] == "NOT_FOUND"
    for p in profiles["items"]:
        assert p["evidence"], p["canonical_url"]
        assert p["limitations"]
        assert len(p["verification_stages"]) >= 2
        for ev in p["evidence"]:
            assert "help@acme.example" not in ev["evidence_summary"]

    # Sorting / filtering
    verified = (await client.get(f"/api/v1/investigations/{inv['id']}/profiles", headers=analyst.headers,
                                 params={"status": "VERIFIED_PUBLIC_REFERENCE"})).json()
    assert verified["total"] == 2
    one = (await client.get(f"/api/v1/profiles/{verified['items'][0]['id']}", headers=analyst.headers))
    assert one.status_code == 200

    # Events were published for websocket subscribers.
    events = [e["type"] for e in await container.events.history(uuid.UUID(inv["id"]))]
    for expected in ("investigation.created", "investigation.started", "discovery.started", "discovery.progress",
                     "profile.discovered", "profile.verified", "investigation.completed"):
        assert expected in events


async def test_idempotency_and_duplicate_prevention(client: httpx.AsyncClient, analyst: UserCtx,
                                                    container: Container) -> None:
    inv = await create_investigation(client, analyst)
    headers = {**analyst.headers, "Idempotency-Key": "run-0001-abcdef"}
    first = await client.post(f"/api/v1/investigations/{inv['id']}/profile-discovery", headers=headers, json={})
    second = await client.post(f"/api/v1/investigations/{inv['id']}/profile-discovery", headers=headers, json={})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("idempotent-replayed") == "true"
    bad = await client.post(f"/api/v1/investigations/{inv['id']}/profile-discovery",
                            headers={**analyst.headers, "Idempotency-Key": "bad key!"}, json={})
    assert bad.status_code == 422


async def test_active_job_blocks_duplicates(client: httpx.AsyncClient, analyst: UserCtx,
                                            container: Container) -> None:
    from app.models import InvestigationJob

    inv = await create_investigation(client, analyst)
    async with container.sessionmaker() as session:
        session.add(InvestigationJob(investigation_id=uuid.UUID(inv["id"]), job_type="profile_discovery",
                                     status="running", parameters={}, progress={}))
        await session.commit()
    r = await run_discovery(client, analyst, inv["id"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "job_already_active"


async def test_cancellation(client: httpx.AsyncClient, analyst: UserCtx, container: Container) -> None:
    from app.models import InvestigationJob
    from app.workers.runner import DiscoveryJobRunner

    inv = await create_investigation(client, analyst)
    async with container.sessionmaker() as session:
        job = InvestigationJob(investigation_id=uuid.UUID(inv["id"]), job_type="profile_discovery", status="queued",
                               parameters={"platforms": ["telegram"], "include_web_search": True, "profile_urls": []},
                               progress={})
        session.add(job)
        await session.commit()
        job_id = job.id
    r = await client.post(f"/api/v1/investigations/{inv['id']}/cancel", headers=analyst.headers)
    assert r.status_code == 202
    outcome = await DiscoveryJobRunner(container).run(job_id)
    assert outcome.status is JobStatus.CANCELLED
    detail = (await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).json()
    assert detail["investigation"]["status"] == "cancelled"
    nothing = await client.post(f"/api/v1/investigations/{inv['id']}/cancel", headers=analyst.headers)
    assert nothing.status_code == 409


async def test_retry_then_dead_letter(client: httpx.AsyncClient, analyst: UserCtx, container: Container) -> None:
    from app.workers.runner import DLQ_KEY

    class Boom:
        providers: list = []

        async def search(self, *args, **kwargs):
            raise RuntimeError("provider exploded")

    container.discovery = Boom()  # type: ignore[assignment]
    inv = await create_investigation(client, analyst)
    r = await run_discovery(client, analyst, inv["id"])
    assert r.status_code == 202
    detail = (await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).json()
    assert detail["investigation"]["status"] == "failed"
    assert detail["latest_job"]["status"] == "dead_letter"
    assert detail["latest_job"]["retry_count"] == container.settings.job_max_tries
    assert detail["latest_job"]["error_code"] == "RuntimeError"
    assert await container.redis.llen(DLQ_KEY) == 1
    events = [e["type"] for e in await container.events.history(uuid.UUID(inv["id"]))]
    assert "investigation.failed" in events


async def test_no_providers_still_works_with_limitations(client: httpx.AsyncClient, analyst: UserCtx,
                                                         container: Container) -> None:
    container.discovery.providers = []
    inv = await create_investigation(client, analyst)
    await run_discovery(client, analyst, inv["id"], profile_urls=["https://t.me/acmehelp"])
    detail = (await client.get(f"/api/v1/investigations/{inv['id']}", headers=analyst.headers)).json()
    assert any("No public search provider" in lim for lim in detail["investigation"]["summary"]["limitations"])


async def test_unknown_platform_rejected(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    inv = await create_investigation(client, analyst)
    r = await client.post(f"/api/v1/investigations/{inv['id']}/profile-discovery", headers=analyst.headers,
                          json={"platforms": ["myspace"]})
    assert r.status_code == 422


async def test_reverify_profile(client: httpx.AsyncClient, analyst: UserCtx, web) -> None:
    inv = await create_investigation(client, analyst)
    await run_discovery(client, analyst, inv["id"])
    profiles = (await client.get(f"/api/v1/investigations/{inv['id']}/profiles", headers=analyst.headers,
                                 params={"platform": "telegram"})).json()["items"]
    tg = profiles[0]
    web.add("https://t.me/acmehelp", httpx.Response(404))
    r = await client.post(f"/api/v1/profiles/{tg['id']}/verify", headers=analyst.headers)
    assert r.status_code == 200
    assert r.json()["verification_status"] == "NOT_FOUND"


async def test_verify_url_endpoint(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    r = await client.post("/api/v1/profiles/verify-url", headers=analyst.headers,
                          json={"url": "https://t.me/acmehelp", "phone": "+919876543210"})
    assert r.status_code == 200
    body = r.json()
    assert body["verification_status"] == "VERIFIED_PUBLIC_REFERENCE"
    assert [s["stage"] for s in body["stages"]][0] == "url_syntax"
    no_phone = (await client.post("/api/v1/profiles/verify-url", headers=analyst.headers,
                                  json={"url": "https://t.me/acmehelp"})).json()
    assert no_phone["verification_status"] == "UNVERIFIED"


async def test_report_and_exports(client: httpx.AsyncClient, analyst: UserCtx) -> None:
    inv = await create_investigation(client, analyst)
    await run_discovery(client, analyst, inv["id"])
    report = (await client.get(f"/api/v1/investigations/{inv['id']}/report", headers=analyst.headers)).json()
    assert report["phone_intelligence"]["normalized_number"] == "+919876543210"
    assert report["summary"]["verified_public_references"] == 2
    assert report["sources"]
    masked = (await client.get(f"/api/v1/investigations/{inv['id']}/report", headers=analyst.headers,
                               params={"mask_phone": True})).json()
    assert masked["phone_intelligence"]["normalized_number"] != "+919876543210"
    for fmt, magic in (("json", b"{"), ("csv", b"\xef\xbb\xbf"), ("html", b"<!doctype html>"), ("pdf", b"%PDF")):
        r = await client.get(f"/api/v1/investigations/{inv['id']}/report/export", headers=analyst.headers,
                             params={"format": fmt})
        assert r.status_code == 200, fmt
        assert r.content.startswith(magic), fmt
        assert "attachment" in r.headers["content-disposition"]
        assert r.headers["cache-control"].startswith("no-store")
    assert (await client.get(f"/api/v1/investigations/{inv['id']}/report/export", headers=analyst.headers,
                             params={"format": "exe"})).status_code == 422
