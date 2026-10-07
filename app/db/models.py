"""Bảng dữ liệu (SQLAlchemy 2). Đổi cột -> thêm migration Alembic mới."""

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    monthly_budget_vnd: Mapped[float | None] = mapped_column(Float)
    # ----- API đối tác (M4); None = không giới hạn / dùng mặc định từ Settings -----
    monthly_page_quota: Mapped[int | None] = mapped_column(Integer)
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    webhook_secret: Mapped[str | None] = mapped_column(String(64))  # ký HMAC webhook ("whsec_...")


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
    source: Mapped[str] = mapped_column(String(16), default="upload")  # upload | zip | api | api_zip
    created_by: Mapped[str | None] = mapped_column(String(320))
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    webhook_state: Mapped[str | None] = mapped_column(String(80))  # trạng thái đã báo qua webhook
    documents: Mapped[list["Document"]] = relationship(back_populates="batch")


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_tenant_sha256", "tenant_id", "sha256"),)
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
    # Tóm tắt bản trích xuất mới nhất (lọc/tô màu danh sách không cần đọc JSON)
    so_gp: Mapped[str | None] = mapped_column(String(64), index=True)
    ngay_cap: Mapped[date | None] = mapped_column(Date, index=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    # ----- API đối tác (M4) -----
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"))
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)  # key ds_test_: kết quả mẫu, 0 lần gọi LLM
    sandbox_scenario: Mapped[str | None] = mapped_column(String(16))
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    webhook_state: Mapped[str | None] = mapped_column(String(80))  # "<trạng thái>:<hash kết quả>" đã gửi
    # Cùng file (sha256) + cùng tenant đã có: không xử lý lại, chép kết quả của document gốc
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    # File đã xóa sau RETENTION_DAYS (kết quả trích xuất vẫn giữ)
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    cost_vnd: Mapped[float] = mapped_column(Float, default=0.0)
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
    cost_vnd: Mapped[float] = mapped_column(Float, default=0.0)
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


class IdempotencyKey(TimestampMixin, Base):
    """Header Idempotency-Key: lưu response lần đầu, gửi lại y hệt cho request trùng (hết hạn sau TTL)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "sandbox", "key"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    key: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 endpoint + tham số + hash file
    status_code: Mapped[int | None] = mapped_column(Integer)  # None = đang xử lý
    response: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)


class WebhookStatus(StrEnum):
    pending = "pending"
    sending = "sending"
    succeeded = "succeeded"
    failed = "failed"  # hết lượt retry


class WebhookDelivery(TimestampMixin, Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(2048))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(16), default=WebhookStatus.pending, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
