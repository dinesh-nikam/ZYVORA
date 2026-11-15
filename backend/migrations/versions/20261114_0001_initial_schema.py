"""Initial PHONEINTEL schema.

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)

# Permission sets are frozen here on purpose: later permission changes must ship
# as their own migration instead of silently rewriting history.
_ANALYST = [
    "phone:validate", "investigation:create", "investigation:read", "investigation:delete",
    "discovery:run", "profile:verify", "report:read", "apikey:manage", "self:data",
]
_ROLES = {
    "admin": [*_ANALYST, "investigation:read_any", "admin:users", "admin:audit", "admin:health"],
    "analyst": _ANALYST,
    "auditor": ["admin:audit", "admin:health", "self:data"],
    "service_telegram": ["telegram:act_as"],
}


def _ts(name: str, *, nullable: bool = True, server_now: bool = False) -> sa.Column:
    return sa.Column(name, TS, nullable=nullable, server_default=sa.func.now() if server_now else None)


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("permissions", JSONType, nullable=False),
        _ts("created_at", nullable=False, server_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        _ts("last_login_at"),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        _ts("locked_until"),
        _ts("deleted_at"),
        _ts("created_at", nullable=False, server_now=True),
        _ts("updated_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_users_role_id_roles", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_status_created_at", "users", ["status", "created_at"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        _ts("expires_at", nullable=False),
        _ts("revoked_at"),
        _ts("created_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id_revoked_at", "refresh_tokens", ["user_id", "revoked_at"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("permissions", JSONType, nullable=False),
        _ts("expires_at"),
        _ts("revoked_at"),
        _ts("last_used_at"),
        _ts("created_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_api_keys_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_user_id_revoked_at", "api_keys", ["user_id", "revoked_at"])

    op.create_table(
        "telegram_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_hash", sa.String(64), nullable=False),
        _ts("created_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_telegram_links_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_telegram_links"),
        sa.UniqueConstraint("user_id", name="uq_telegram_links_user_id"),
        sa.UniqueConstraint("telegram_user_hash", name="uq_telegram_links_telegram_user_hash"),
    )

    op.create_table(
        "investigations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("investigation_reference", sa.String(32), nullable=False),
        sa.Column("normalized_phone", sa.Text(), nullable=True),
        sa.Column("phone_lookup_hash", sa.String(64), nullable=True),
        sa.Column("phone_country_code", sa.Integer(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("authorization_basis", sa.String(48), nullable=False),
        sa.Column("authorization_reference", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", JSONType, nullable=False),
        _ts("started_at"),
        _ts("completed_at"),
        _ts("expires_at", nullable=False),
        _ts("deleted_at"),
        _ts("created_at", nullable=False, server_now=True),
        _ts("updated_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_investigations_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_investigations"),
        sa.UniqueConstraint("investigation_reference", name="uq_investigations_investigation_reference"),
    )
    op.create_index("ix_investigations_user_id_created_at", "investigations", ["user_id", "created_at"])
    op.create_index("ix_investigations_user_id_status", "investigations", ["user_id", "status"])
    op.create_index("ix_investigations_user_id_phone_lookup_hash", "investigations", ["user_id", "phone_lookup_hash"])
    op.create_index("ix_investigations_expires_at", "investigations", ["expires_at"])
    op.create_index("ix_investigations_deleted_at", "investigations", ["deleted_at"])

    op.create_table(
        "phone_intelligence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("country_code", sa.Integer(), nullable=True),
        sa.Column("region_code", sa.String(8), nullable=True),
        sa.Column("country_name", sa.String(128), nullable=True),
        sa.Column("number_type", sa.String(32), nullable=False),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("carrier", sa.String(128), nullable=True),
        sa.Column("metadata_provider", sa.String(128), nullable=False),
        sa.Column("details", JSONType, nullable=False),
        _ts("checked_at", nullable=False),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"],
                                name="fk_phone_intelligence_investigation_id_investigations", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_phone_intelligence"),
        sa.UniqueConstraint("investigation_id", name="uq_phone_intelligence_investigation_id"),
    )

    op.create_table(
        "investigation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("parameters", JSONType, nullable=False),
        sa.Column("progress", JSONType, nullable=False),
        _ts("cancel_requested_at"),
        _ts("started_at"),
        _ts("completed_at"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, server_now=True),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"],
                                name="fk_investigation_jobs_investigation_id_investigations", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_investigation_jobs"),
    )
    op.create_index("ix_investigation_jobs_investigation_id_created_at", "investigation_jobs",
                    ["investigation_id", "created_at"])
    op.create_index("ix_investigation_jobs_status", "investigation_jobs", ["status"])
    active = sa.text("status IN ('queued', 'running', 'retrying')")
    op.create_index("uq_investigation_jobs_active", "investigation_jobs", ["investigation_id", "job_type"],
                    unique=True, postgresql_where=active, sqlite_where=active)

    op.create_table(
        "discovered_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("profile_url", sa.String(2048), nullable=False),
        sa.Column("canonical_url", sa.String(2048), nullable=False),
        sa.Column("handle", sa.String(255), nullable=True),
        sa.Column("discovery_source", sa.String(48), nullable=False),
        sa.Column("evidence_description", sa.Text(), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("confidence_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence_level", sa.String(8), nullable=False),
        sa.Column("confidence_factors", JSONType, nullable=False),
        sa.Column("verification_stages", JSONType, nullable=False),
        sa.Column("limitations", JSONType, nullable=False),
        _ts("discovered_at", nullable=False),
        _ts("verified_at"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"],
                                name="fk_discovered_profiles_investigation_id_investigations", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_discovered_profiles"),
        sa.UniqueConstraint("investigation_id", "canonical_url", name="uq_discovered_profiles_investigation_canonical"),
    )
    op.create_index("ix_discovered_profiles_investigation_id_platform", "discovered_profiles",
                    ["investigation_id", "platform"])
    op.create_index("ix_discovered_profiles_investigation_id_status", "discovered_profiles",
                    ["investigation_id", "verification_status"])

    op.create_table(
        "evidence_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("discovered_profile_id", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(64), nullable=False),
        sa.Column("source_reliability", sa.String(16), nullable=False),
        sa.Column("evidence_summary", sa.Text(), nullable=False),
        sa.Column("phone_reference_found", sa.Boolean(), nullable=False),
        _ts("collected_at", nullable=False),
        _ts("expiration_at", nullable=False),
        sa.ForeignKeyConstraint(["discovered_profile_id"], ["discovered_profiles.id"],
                                name="fk_evidence_sources_discovered_profile_id_discovered_profiles", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_sources"),
    )
    op.create_index("ix_evidence_sources_discovered_profile_id", "evidence_sources", ["discovered_profile_id"])
    op.create_index("ix_evidence_sources_expiration_at", "evidence_sources", ["expiration_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(48), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=True),
        _ts("timestamp", nullable=False, server_now=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("metadata", JSONType, nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_audit_logs_actor_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])
    op.create_index("ix_audit_logs_actor_id_timestamp", "audit_logs", ["actor_id", "timestamp"])
    op.create_index("ix_audit_logs_resource_type_resource_id", "audit_logs", ["resource_type", "resource_id"])
    op.create_index("ix_audit_logs_action_timestamp", "audit_logs", ["action", "timestamp"])

    if op.get_bind().dialect.name == "postgresql":
        # Audit log rows are append-only: block UPDATE (DELETE stays allowed for retention purges).
        op.execute(
            """
            CREATE OR REPLACE FUNCTION audit_logs_block_update() RETURNS trigger AS $$
            BEGIN
                IF NEW.actor_id IS DISTINCT FROM OLD.actor_id AND NEW.actor_id IS NULL
                   AND NEW.action = OLD.action AND NEW.resource_type = OLD.resource_type
                   AND NEW.resource_id IS NOT DISTINCT FROM OLD.resource_id
                   AND NEW.timestamp = OLD.timestamp THEN
                    RETURN NEW;  -- permitted: ON DELETE SET NULL of the actor FK
                END IF;
                RAISE EXCEPTION 'audit_logs is append-only';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        op.execute(
            "CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE ON audit_logs "
            "FOR EACH ROW EXECUTE FUNCTION audit_logs_block_update();"
        )

    now = datetime.now(UTC)
    op.bulk_insert(roles, [
        {"id": uuid.uuid4(), "name": name, "permissions": perms, "created_at": now}
        for name, perms in _ROLES.items()
    ])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_block_update();")
    for table in (
        "audit_logs", "evidence_sources", "discovered_profiles", "investigation_jobs", "phone_intelligence",
        "investigations", "telegram_links", "api_keys", "refresh_tokens", "users", "roles",
    ):
        op.drop_table(table)
