"""Async engine / session factory and transaction helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings


def create_engine(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    dsn = url or settings.database_url.get_secret_value()
    kwargs: dict[str, object] = {"echo": settings.database_echo, "pool_pre_ping": True}
    if dsn.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(pool_size=settings.database_pool_size, max_overflow=settings.database_max_overflow)
    engine = create_async_engine(dsn, **kwargs)
    if dsn.startswith("sqlite"):
        _enable_sqlite_fk(engine.sync_engine)
    return engine


def _enable_sqlite_fk(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Open a session, commit on success, roll back on any exception."""
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
