from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, UTCDateTime, UUIDPrimaryKey, utcnow


class AuditLog(UUIDPrimaryKey, Base):
    """Append-only audit trail.  Never stores phone numbers or secrets."""

    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    # ``metadata`` is reserved by SQLAlchemy's declarative API.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_logs_timestamp", "timestamp"),
        Index("ix_audit_logs_actor_id_timestamp", "actor_id", "timestamp"),
        Index("ix_audit_logs_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_audit_logs_action_timestamp", "action", "timestamp"),
    )
