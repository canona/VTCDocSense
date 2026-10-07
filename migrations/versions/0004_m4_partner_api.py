"""m4 partner api: sandbox, webhook, idempotency, hạn mức tenant, retention

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB_VARIANT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
FALSE = sa.false()


def upgrade() -> None:
    op.add_column("tenants", sa.Column("monthly_page_quota", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("webhook_secret", sa.String(length=64), nullable=True))

    op.add_column("batches", sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column("batches", sa.Column("webhook_url", sa.String(length=2048), nullable=True))
    op.add_column("batches", sa.Column("webhook_state", sa.String(length=80), nullable=True))

    op.add_column("documents", sa.Column("api_key_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=FALSE))
    op.add_column("documents", sa.Column("sandbox_scenario", sa.String(length=16), nullable=True))
    op.add_column("documents", sa.Column("webhook_url", sa.String(length=2048), nullable=True))
    op.add_column("documents", sa.Column("webhook_state", sa.String(length=80), nullable=True))
    op.add_column("documents", sa.Column("duplicate_of", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_documents_api_key_id", "documents", "api_keys", ["api_key_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_documents_duplicate_of", "documents", "documents", ["duplicate_of"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_documents_duplicate_of"), "documents", ["duplicate_of"], unique=False)
    op.create_index("ix_documents_tenant_sha256", "documents", ["tenant_id", "sha256"], unique=False)

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response", JSONB_VARIANT, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "sandbox", "key"),
    )
    op.create_index(op.f("ix_idempotency_keys_tenant_id"), "idempotency_keys", ["tenant_id"], unique=False)

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("batch_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("payload", JSONB_VARIANT, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("tenant_id", "document_id", "batch_id", "status", "next_attempt_at"):
        op.create_index(op.f(f"ix_webhook_deliveries_{col}"), "webhook_deliveries", [col], unique=False)


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_documents_tenant_sha256", table_name="documents")
    op.drop_index(op.f("ix_documents_duplicate_of"), table_name="documents")
    op.drop_constraint("fk_documents_duplicate_of", "documents", type_="foreignkey")
    op.drop_constraint("fk_documents_api_key_id", "documents", type_="foreignkey")
    for col in (
        "purged_at",
        "duplicate_of",
        "webhook_state",
        "webhook_url",
        "sandbox_scenario",
        "sandbox",
        "api_key_id",
    ):
        op.drop_column("documents", col)
    for col in ("webhook_state", "webhook_url", "sandbox"):
        op.drop_column("batches", col)
    for col in ("webhook_secret", "rate_limit_per_minute", "monthly_page_quota"):
        op.drop_column("tenants", col)
