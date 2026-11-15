from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, EncryptedText, JSONType, Timestamps, UTCDateTime, UUIDPrimaryKey, utcnow
from app.models.enums import InvestigationStatus, JobStatus

if TYPE_CHECKING:
    from app.models.profile import DiscoveredProfile
    from app.models.user import User


class Investigation(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "investigations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    investigation_reference: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    # AES-256-GCM encrypted E.164 number; NULL after erasure.
    normalized_phone: Mapped[str | None] = mapped_column(EncryptedText())
    # HMAC blind index of the E.164 number for equality lookups.
    phone_lookup_hash: Mapped[str | None] = mapped_column(String(64))
    phone_country_code: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str | None] = mapped_column(EncryptedText())
    authorization_basis: Mapped[str] = mapped_column(String(48), nullable=False)
    authorization_reference: Mapped[str | None] = mapped_column(EncryptedText())
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=InvestigationStatus.CREATED.value)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    user: Mapped[User] = relationship(back_populates="investigations")
    phone_intelligence: Mapped[PhoneIntelligence | None] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )
    profiles: Mapped[list[DiscoveredProfile]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    jobs: Mapped[list[InvestigationJob]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_investigations_user_id_created_at", "user_id", "created_at"),
        Index("ix_investigations_user_id_status", "user_id", "status"),
        Index("ix_investigations_user_id_phone_lookup_hash", "user_id", "phone_lookup_hash"),
        Index("ix_investigations_expires_at", "expires_at"),
        Index("ix_investigations_deleted_at", "deleted_at"),
    )


class PhoneIntelligence(UUIDPrimaryKey, Base):
    __tablename__ = "phone_intelligence"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    country_code: Mapped[int | None] = mapped_column(Integer)
    region_code: Mapped[str | None] = mapped_column(String(8))
    country_name: Mapped[str | None] = mapped_column(String(128))
    number_type: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    carrier: Mapped[str | None] = mapped_column(String(128))
    metadata_provider: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    investigation: Mapped[Investigation] = relationship(back_populates="phone_intelligence")


class InvestigationJob(UUIDPrimaryKey, Base):
    __tablename__ = "investigation_jobs"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED.value)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, server_default=func.now(), nullable=False
    )

    investigation: Mapped[Investigation] = relationship(back_populates="jobs")

    __table_args__ = (
        Index("ix_investigation_jobs_investigation_id_created_at", "investigation_id", "created_at"),
        Index("ix_investigation_jobs_status", "status"),
        # Duplicate-job prevention: at most one active job per investigation and type.
        Index(
            "uq_investigation_jobs_active",
            "investigation_id",
            "job_type",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'retrying')"),
            sqlite_where=text("status IN ('queued', 'running', 'retrying')"),
        ),
    )
