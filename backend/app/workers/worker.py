"""arq worker entrypoint:  ``arq app.workers.worker.WorkerSettings``

* retries with exponential backoff (``Retry(defer=...)``)
* hard job timeout, bounded concurrency (``max_jobs``)
* dead-letter list for jobs that exhaust their retries
* daily retention purge (cron)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from typing import Any

from arq import Retry, cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import DEAD_LETTER_JOBS
from app.models.enums import JobStatus
from app.services.container import Container, build_container
from app.services.retention import purge_expired
from app.workers.queue import DISCOVERY_TASK
from app.workers.runner import DLQ_KEY, DiscoveryJobRunner

logger = logging.getLogger(__name__)
_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging(_settings.log_level, json_output=_settings.log_json)
    container = await build_container(_settings)
    ctx["container"] = container
    ctx["runner"] = DiscoveryJobRunner(container)
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    container: Container | None = ctx.get("container")
    if container is not None:
        await container.aclose()


async def run_profile_discovery(ctx: dict[str, Any], job_id: str) -> str:
    runner: DiscoveryJobRunner = ctx["runner"]
    attempt = int(ctx.get("job_try", 1))
    outcome = await runner.run(uuid.UUID(job_id), attempt)
    if outcome.status is JobStatus.RETRYING:
        raise Retry(defer=outcome.retry_in or runner.backoff(attempt))
    if outcome.status is JobStatus.DEAD_LETTER:
        container: Container = ctx["container"]
        DEAD_LETTER_JOBS.set(await container.redis.llen(DLQ_KEY))  # type: ignore[misc]
    return outcome.status.value


async def retention_purge(ctx: dict[str, Any]) -> dict[str, int]:
    container: Container = ctx["container"]
    report = await purge_expired(container.sessionmaker, container.settings)
    return asdict(report)


if run_profile_discovery.__name__ != DISCOVERY_TASK:  # task name used by ArqJobQueue
    raise RuntimeError("Worker task name mismatch")


class WorkerSettings:
    functions = [run_profile_discovery]
    cron_jobs = [cron(retention_purge, hour={3}, minute={17}, run_at_startup=False, unique=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url.get_secret_value())
    max_jobs = _settings.worker_max_jobs
    job_timeout = _settings.job_timeout_seconds + 30
    max_tries = _settings.job_max_tries
    keep_result = 3600
    allow_abort_jobs = True
    health_check_interval = 30
