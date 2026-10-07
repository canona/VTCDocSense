"""Bảng dữ liệu (SQLAlchemy 2). Đổi cột -> thêm migration Alembic mới."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

# JSONB trên Postgres, JSON trên SQLite (test)
JsonType = JSON().with_variant(JSONB(), "postgresql")

DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Role(StrEnum):
    viewer = "viewer"
    reviewer = "reviewer"
    admin = "admin"


class DocStatus(StrEnum):
    uploaded = "uploaded"
    processing = "processing"
    needs_review = "needs_review"
    auto_approved = "auto_approved"
    approved = "approved"
    rejected = "rejected"
    failed = "failed"  # lỗi PDF/provider; xem documents.error


FINAL_REVIEW_STATUSES = {DocStatus.approved, DocStatus.rejected}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    require_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(16), default=Role.viewer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    prefix: Mapped[str] = mapped_column(String(32), index=True)  # vd "ds_live_ab12"
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JsonType, default=list)
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Batch(TimestampMixin, Base):
    __tablename__ = "batches"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="upload")  # upload | zip
    created_by: Mapped[str | None] = mapped_column(String(320))
    documents: Mapped[list["Document"]] = relationship(back_populates="batch")


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"), index=True)
    folder_name: Mapped[str | None] = mapped_column(String(512), index=True)
    file_name: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(1024))
    external_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default=DocStatus.uploaded, index=True)
    error: Mapped[str | None] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(String(64))
    pages: Mapped[int | None] = mapped_column(Integer)
    pdf_type: Mapped[str | None] = mapped_column(String(8))
    loai_van_ban: Mapped[str | None] = mapped_column(String(64))
    # GP cũ được trích dẫn trong lớp chữ: [{"so_gp": "...", "ngay": "yyyy-mm-dd"}] (đối chiếu chéo)
    text_refs: Mapped[list[dict[str, str]]] = mapped_column(JsonType, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    batch: Mapped[Batch | None] = relationship(back_populates="documents")


class Extraction(TimestampMixin, Base):
    __tablename__ = "extractions"
    __table_args__ = (UniqueConstraint("document_id", "version"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)  # GiayPhep (schema v1)
    schema_version: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(320))  # None = pipeline


class FieldReview(TimestampMixin, Base):
    __tablename__ = "field_reviews"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extractions.id", ondelete="CASCADE"))
    field_path: Mapped[str] = mapped_column(String(255), index=True)
    old_value: Mapped[Any] = mapped_column(JsonType, nullable=True)
    new_value: Mapped[Any] = mapped_column(JsonType, nullable=True)
    user_email: Mapped[str] = mapped_column(String(320))


class LlmCall(TimestampMixin, Base):
    __tablename__ = "llm_calls"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    schema_name: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(TimestampMixin, Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor: Mapped[str] = mapped_column(String(320))  # email | "api_key:<prefix>" | "system"
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
