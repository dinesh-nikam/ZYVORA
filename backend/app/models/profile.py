from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, EncryptedText, JSONType, UTCDateTime, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.investigation import Investigation


class DiscoveredProfile(UUIDPrimaryKey, Base):
    __tablename__ = "discovered_profiles"

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    handle: Mapped[str | None] = mapped_column(String(255))
    discovery_source: Mapped[str] = mapped_column(String(48), nullable=False)
    evidence_description: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence_level: Mapped[str] = mapped_column(String(8), nullable=False)
    confidence_factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    verification_stages: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    limitations: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    investigation: Mapped[Investigation] = relationship(back_populates="profiles")
    evidence: Mapped[list[EvidenceSource]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", passive_deletes=True,
        order_by="EvidenceSource.collected_at",
    )

    __table_args__ = (
        UniqueConstraint("investigation_id", "canonical_url", name="uq_discovered_profiles_investigation_canonical"),
        Index("ix_discovered_profiles_investigation_id_platform", "investigation_id", "platform"),
        Index("ix_discovered_profiles_investigation_id_status", "investigation_id", "verification_status"),
    )


class EvidenceSource(UUIDPrimaryKey, Base):
    __tablename__ = "evidence_sources"

    discovered_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovered_profiles.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_reliability: Mapped[str] = mapped_column(String(16), nullable=False)
    # Redacted excerpt; encrypted at rest.
    evidence_summary: Mapped[str] = mapped_column(EncryptedText(), nullable=False)
    phone_reference_found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expiration_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    profile: Mapped[DiscoveredProfile] = relationship(back_populates="evidence")

    __table_args__ = (
        Index("ix_evidence_sources_discovered_profile_id", "discovered_profile_id"),
        Index("ix_evidence_sources_expiration_at", "expiration_at"),
    )
