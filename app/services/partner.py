"""API đối tác: trạng thái công khai, dữ liệu trả về (đã lọc thông tin nội bộ), hạn mức và usage.

Trạng thái công khai (khác trạng thái nội bộ `DocStatus`):

| nội bộ                        | require_human_review=true | require_human_review=false |
|-------------------------------|---------------------------|----------------------------|
| uploaded                      | queued                    | queued                     |
| processing                    | processing                | processing                 |
| needs_review, auto_approved   | pending_review            | completed (+ needs_review) |
| approved                      | completed                 | completed                  |
| rejected / failed             | rejected / failed         | rejected / failed          |
"""

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Batch, DocStatus, Document, Extraction, LlmCall, Tenant

VN_TZ = timezone(timedelta(hours=7))  # kỳ tính usage/hạn mức theo tháng giờ Việt Nam


class PublicStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    pending_review = "pending_review"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"


FINAL_PUBLIC = {PublicStatus.completed, PublicStatus.rejected, PublicStatus.failed}
SANDBOX_SCENARIOS = ("completed", "failed", "rejected")
# Khóa meta nội bộ, không trả cho đối tác
_META_INTERNAL = ("provider", "model", "duration_ms", "input_tokens", "output_tokens", "classified_by")


def public_status(doc: Document, require_review: bool) -> PublicStatus:
    s = doc.status
    if s == DocStatus.uploaded:
        return PublicStatus.queued
    if s == DocStatus.processing:
        return PublicStatus.processing
    if s in (DocStatus.needs_review, DocStatus.auto_approved):
        return PublicStatus.pending_review if require_review else PublicStatus.completed
    if s == DocStatus.approved:
        return PublicStatus.completed
    if s == DocStatus.rejected:
        return PublicStatus.rejected
    return PublicStatus.failed


def _scrub(node: Any) -> Any:
    """verified_by "ra_soat:<email>" -> "ra_soat" (không lộ email người duyệt nội bộ)."""
    if isinstance(node, dict):
        out = {k: _scrub(v) for k, v in node.items()}
        vb = out.get("verified_by")
        if isinstance(vb, str) and vb.startswith("ra_soat:"):
            out["verified_by"] = "ra_soat"
        return out
    if isinstance(node, list):
        return [_scrub(v) for v in node]
    return node


def public_result(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = _scrub(copy.deepcopy(data))
    meta = out.get("meta")
    if isinstance(meta, dict):
        for k in _META_INTERNAL:
            meta.pop(k, None)
    return out


def result_hash(data: dict[str, Any] | None) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def public_error(doc: Document) -> dict[str, str] | None:
    if doc.status != DocStatus.failed:
        return None
    if (doc.error or "").startswith("PdfError"):
        return {"code": "invalid_pdf", "message": "Không đọc được PDF (hỏng, có mật khẩu hoặc vượt giới hạn)"}
    return {"code": "processing_failed", "message": "Xử lý thất bại; liên hệ VTCDocSense kèm id document"}


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def document_view(
    doc: Document,
    require_review: bool,
    ext: Extraction | None = None,
    *,
    include_result: bool = True,
) -> dict[str, Any]:
    st = public_status(doc, require_review)
    released = st == PublicStatus.completed and ext is not None
    view: dict[str, Any] = {
        "id": str(doc.id),
        "object": "document",
        "external_id": doc.external_id,
        "batch_id": str(doc.batch_id) if doc.batch_id else None,
        "file_name": doc.file_name,
        "folder_name": doc.folder_name,
        "sha256": doc.sha256,
        "status": st.value,
        # Chỉ có nghĩa khi completed mà chưa qua người duyệt (require_human_review=false)
        "needs_review": doc.status == DocStatus.needs_review if st == PublicStatus.completed else None,
        "document_type": doc.loai_van_ban,
        "pages": doc.pages,
        "pdf_type": doc.pdf_type,
        "sandbox": doc.sandbox,
        "deduplicated": doc.duplicate_of is not None,
        "created_at": iso(doc.created_at),
        "updated_at": iso(doc.updated_at),
        "error": public_error(doc),
        "result_version": ext.version if released and ext else None,
    }
    if include_result:
        view["result"] = public_result(ext.data) if released and ext else None
    return view


def batch_status(statuses: list[PublicStatus]) -> str:
    return "completed" if statuses and all(s in FINAL_PUBLIC for s in statuses) else "processing"


def batch_view(batch: Batch, docs: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for d in docs:
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    return {
        "id": str(batch.id),
        "object": "batch",
        "name": batch.name,
        "status": batch_status([PublicStatus(d["status"]) for d in docs]),
        "sandbox": batch.sandbox,
        "created_at": iso(batch.created_at),
        "counts": counts,
        "documents": docs,
    }


# ---------------- Usage & hạn mức ----------------


def month_range(period: str | None = None) -> tuple[str, datetime, datetime]:
    """period "YYYY-MM" (giờ VN) -> (period, đầu kỳ UTC, đầu kỳ sau UTC)."""
    if period:
        y, m = (int(x) for x in period.split("-"))
    else:
        now = datetime.now(VN_TZ)
        y, m = now.year, now.month
    start = datetime(y, m, 1, tzinfo=VN_TZ)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=VN_TZ)
    return f"{y:04d}-{m:02d}", start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class Usage:
    period: str
    documents: int
    pages: int
    cost_vnd: float
    llm_calls: int


async def usage(
    session: AsyncSession, tenant_id: uuid.UUID, sandbox: bool, period: str | None = None
) -> Usage:
    """Trang tính theo document đã nhận trong kỳ (không gồm bản trùng - không xử lý lại)."""
    label, start, end = month_range(period)
    docs, pages = (
        await session.execute(
            select(func.count(Document.id), func.coalesce(func.sum(Document.pages), 0)).where(
                Document.tenant_id == tenant_id,
                Document.sandbox == sandbox,
                Document.duplicate_of.is_(None),
                Document.created_at >= start,
                Document.created_at < end,
            )
        )
    ).one()
    calls, cost = (
        await session.execute(
            select(func.count(LlmCall.id), func.coalesce(func.sum(LlmCall.cost_vnd), 0.0))
            .join(Document, Document.id == LlmCall.document_id)
            .where(
                LlmCall.tenant_id == tenant_id,
                Document.sandbox == sandbox,
                LlmCall.live.is_(True),
                LlmCall.created_at >= start,
                LlmCall.created_at < end,
            )
        )
    ).one()
    return Usage(label, int(docs or 0), int(pages or 0), round(float(cost or 0), 1), int(calls or 0))


def rate_limit(tenant: Tenant, settings: Settings) -> int:
    return tenant.rate_limit_per_minute or settings.api_rate_limit_per_minute


@dataclass
class QuotaError:
    code: str  # page_quota_exceeded | budget_exceeded
    message: str


def check_quota(tenant: Tenant, used: Usage, new_pages: int, est_cost_vnd: float | None) -> QuotaError | None:
    """Kiểm tra trước khi nhận file (chỉ key live; sandbox không tính)."""
    q = tenant.monthly_page_quota
    if q is not None and used.pages + new_pages > q:
        return QuotaError(
            "page_quota_exceeded",
            f"Vượt hạn mức {q} trang/tháng (đã dùng {used.pages}, yêu cầu thêm {new_pages})",
        )
    b = tenant.monthly_budget_vnd
    if b is not None and used.cost_vnd + (est_cost_vnd or 0) > b:
        return QuotaError(
            "budget_exceeded",
            f"Vượt hạn mức {b:,.0f} đ/tháng (đã dùng {used.cost_vnd:,.0f} đ, "
            f"ước tính thêm {est_cost_vnd or 0:,.0f} đ)",
        )
    return None
