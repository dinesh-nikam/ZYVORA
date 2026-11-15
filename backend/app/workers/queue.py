"""Job queue abstraction.

* :class:`ArqJobQueue` - production: Redis-backed arq queue with deterministic
  job ids (duplicate-enqueue prevention), retries with exponential backoff,
  abort support.
* :class:`InlineJobQueue` - development/test: executes jobs in-process with the
  same runner and the same retry semantics.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.models.enums import JobStatus

logger = logging.getLogger(__name__)

DISCOVERY_TASK = "run_profile_discovery"


def discovery_job_key(job_id: uuid.UUID) -> str:
    return f"discovery:{job_id}"


class JobQueue(Protocol):
    async def enqueue_discovery(self, job_id: uuid.UUID) -> None: ...

    async def abort(self, job_id: uuid.UUID) -> None: ...

    async def aclose(self) -> None: ...


class ArqJobQueue:
    def __init__(self, redis_dsn: str) -> None:
        self._dsn = redis_dsn
        self._pool: object | None = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            from arq import create_pool  # noqa: PLC0415
            from arq.connections import RedisSettings  # noqa: PLC0415

            self._pool = await create_pool(RedisSettings.from_dsn(self._dsn))
        return self._pool

    async def enqueue_discovery(self, job_id: uuid.UUID) -> None:
        pool = await self._get_pool()
        job = await pool.enqueue_job(DISCOVERY_TASK, str(job_id), _job_id=discovery_job_key(job_id))
        if job is None:
            logger.info("discovery job already enqueued", extra={"job_id": str(job_id)})

    async def abort(self, job_id: uuid.UUID) -> None:
        from arq.jobs import Job  # noqa: PLC0415

        pool = await self._get_pool()
        await Job(discovery_job_key(job_id), pool).abort(timeout=0)

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()  # type: ignore[attr-defined]


Runner = Callable[[uuid.UUID, int], Awaitable["object"]]


class InlineJobQueue:
    """Runs jobs in the current process.  ``eager=True`` awaits completion inside
    ``enqueue_discovery`` (deterministic tests); otherwise jobs run as tasks."""

    def __init__(self, runner: Callable[[uuid.UUID, int], Awaitable[object]], *, eager: bool = False,
                 max_tries: int = 4) -> None:
        self._runner = runner
        self._eager = eager
        self._max_tries = max_tries
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def _execute(self, job_id: uuid.UUID) -> None:
        attempt = 1
        while True:
            outcome = await self._runner(job_id, attempt)
            status = getattr(outcome, "status", None)
            retry_in = getattr(outcome, "retry_in", None)
            if status is not JobStatus.RETRYING or attempt >= self._max_tries:
                return
            await asyncio.sleep(retry_in or 0)
            attempt += 1

    async def enqueue_discovery(self, job_id: uuid.UUID) -> None:
        if self._eager:
            await self._execute(job_id)
            return
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        task = asyncio.create_task(self._execute(job_id), name=f"discovery-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None))

    async def abort(self, job_id: uuid.UUID) -> None:
        # Cooperative: the runner observes ``cancel_requested_at`` between steps.
        return None

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
