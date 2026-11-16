from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.repositories.base import Page, apply_sort, paginate


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, entry: AuditLog) -> None:
        self.session.add(entry)

    async def list(
        self,
        *,
        actor_id: uuid.UUID | None,
        action: str | None,
        resource_type: str | None,
        resource_id: str | None,
        since: datetime | None,
        until: datetime | None,
        sort: str | None,
        page: int,
        page_size: int,
    ) -> Page[AuditLog]:
        stmt = select(AuditLog)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id:
            stmt = stmt.where(AuditLog.resource_id == resource_id)
        if since:
            stmt = stmt.where(AuditLog.timestamp >= since)
        if until:
            stmt = stmt.where(AuditLog.timestamp <= until)
        stmt = apply_sort(stmt, sort, {"timestamp": AuditLog.timestamp, "action": AuditLog.action}, "-timestamp")
        return await paginate(self.session, stmt, page, page_size)
