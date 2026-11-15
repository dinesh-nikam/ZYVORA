"""Operator commands.

    python -m app.manage generate-keys
    python -m app.manage create-admin --email admin@example.org
    python -m app.manage create-service-account --name telegram-bot
    python -m app.manage purge
    python -m app.manage dlq [--requeue]
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys

from app.core.config import JobBackend, get_settings
from app.core.crypto import generate_field_key
from app.core.permissions import BUILTIN_ROLES
from app.services.audit import AuditContext
from app.services.auth import AuthService, Principal
from app.services.container import build_container
from app.services.retention import purge_expired
from app.workers.runner import DLQ_KEY


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")


async def _create_user(email: str, role: str, password: str | None) -> None:
    container = await build_container(get_settings())
    try:
        async with container.sessionmaker() as session:
            auth = AuthService(session, container.settings, container.tokens, container.redis, container.indexer)
            pwd = password or getpass.getpass("Password (min 12 chars): ")
            user = await auth.register(email, pwd, AuditContext(None, None, "system"), role_name=role,
                                       status="active", bypass_closed=True)
            await session.commit()
            _print(f"Created {role} user {user.email} ({user.id})")
    finally:
        await container.aclose()


async def _create_service_account(name: str) -> None:
    container = await build_container(get_settings())
    try:
        async with container.sessionmaker() as session:
            auth = AuthService(session, container.settings, container.tokens, container.redis, container.indexer)
            email = f"svc-{name}@service.phoneintel.invalid"
            user = await auth.register(email, secrets.token_urlsafe(48), AuditContext(None, None, "system"),
                                       role_name="service_telegram", status="active", bypass_closed=True)
            principal = Principal(user=user, permissions=BUILTIN_ROLES["service_telegram"], via="system")
            _, plaintext = await auth.create_api_key(principal, f"{name} service key", None, 365,
                                                     AuditContext(user.id, None, "system"))
            await session.commit()
            _print(f"Service account {email} created.")
            _print("API key (shown once; store it in the bot's secret store as BOT_SERVICE_API_KEY):")
            _print(plaintext)
    finally:
        await container.aclose()


async def _purge() -> None:
    container = await build_container(get_settings())
    try:
        report = await purge_expired(container.sessionmaker, container.settings)
        _print(f"Retention purge: {report}")
    finally:
        await container.aclose()


async def _dlq(requeue: bool) -> None:
    import uuid  # noqa: PLC0415

    from app.db.session import transaction  # noqa: PLC0415
    from app.models.enums import JobStatus  # noqa: PLC0415
    from app.repositories.investigations import JobRepository  # noqa: PLC0415

    settings = get_settings()
    container = await build_container(settings)
    try:
        ids = await container.redis.lrange(DLQ_KEY, 0, -1)
        _print(f"{len(ids)} job(s) in dead-letter queue")
        for raw in ids:
            _print(f"  {raw}")
        if requeue and ids:
            if settings.job_backend is not JobBackend.ARQ:
                _print("Requeue requires JOB_BACKEND=arq")
                return
            for raw in ids:
                job_id = uuid.UUID(raw)
                async with transaction(container.sessionmaker) as session:
                    job = await JobRepository(session).get(job_id, for_update=True)
                    if job is None or job.status != JobStatus.DEAD_LETTER.value:
                        continue
                    job.status = JobStatus.QUEUED.value
                    job.error_code = None
                await container.jobs.enqueue_discovery(job_id)
                await container.redis.lrem(DLQ_KEY, 0, raw)
                _print(f"  requeued {raw}")
    finally:
        await container.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-keys", help="print fresh secrets for .env")
    admin = sub.add_parser("create-admin")
    admin.add_argument("--email", required=True)
    admin.add_argument("--role", default="admin", choices=sorted(BUILTIN_ROLES))
    svc = sub.add_parser("create-service-account")
    svc.add_argument("--name", required=True)
    sub.add_parser("purge", help="apply retention policies now")
    dlq = sub.add_parser("dlq", help="inspect (and optionally requeue) dead-letter jobs")
    dlq.add_argument("--requeue", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "generate-keys":
        _print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        _print(f"BLIND_INDEX_KEY={secrets.token_urlsafe(48)}")
        _print(f"FIELD_ENCRYPTION_KEYS={generate_field_key('k1')}")
        _print(f"METRICS_TOKEN={secrets.token_urlsafe(32)}")
        return 0
    if args.command == "create-admin":
        asyncio.run(_create_user(args.email, args.role, None))
    elif args.command == "create-service-account":
        asyncio.run(_create_service_account(args.name))
    elif args.command == "purge":
        asyncio.run(_purge())
    elif args.command == "dlq":
        asyncio.run(_dlq(args.requeue))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
