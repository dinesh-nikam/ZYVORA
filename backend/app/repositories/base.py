"""Shared repository helpers: allowlisted sorting and pagination."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailed

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


def apply_sort(stmt: Select[Any], sort: str | None, allowed: dict[str, Any], default: str) -> Select[Any]:
    """Apply ``sort`` like ``-created_at``; only allowlisted fields are accepted."""
    spec = (sort or default).strip()
    descending = spec.startswith("-")
    field = spec.lstrip("-+")
    column = allowed.get(field)
    if column is None:
        raise ValidationFailed("Unsupported sort field", code="invalid_sort",
                               details=[{"allowed": sorted(allowed)}])
    return stmt.order_by(column.desc() if descending else column.asc())


async def paginate(session: AsyncSession, stmt: Select[Any], page: int, page_size: int) -> Page[Any]:
    total = int((await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery())))
                .scalar_one())
    rows = (await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))).unique().scalars().all()
    return Page(items=list(rows), total=total, page=page, page_size=page_size)
