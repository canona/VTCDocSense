"""Chống xử lý trùng: cùng file (sha256) + cùng tenant (+ cùng chế độ sandbox/live) -> không gọi LLM lại.

Document mới vẫn được tạo (id riêng, external_id/batch/webhook riêng) nhưng `duplicate_of` trỏ về document
gốc và nhận bản sao kết quả của gốc:
- gốc đã có kết quả -> chép ngay;
- gốc đang xử lý   -> document mới ở `processing`, chép khi gốc xong (trong worker);
- gốc được rà soát (duyệt/sửa/từ chối) -> chép lại cho các bản trùng chưa được người duyệt riêng.
"""

import copy
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocStatus, Document, Extraction
from app.models.schema import SCHEMA_VERSION
from app.services import webhooks
from app.services.queries import latest_extraction
from app.services.review import sync_summary

# Trạng thái của gốc đủ để chép (đã có kết quả cuối của pipeline hoặc người duyệt)
_READY = {
    DocStatus.needs_review,
    DocStatus.auto_approved,
    DocStatus.approved,
    DocStatus.rejected,
    DocStatus.failed,
}
# Bản trùng còn nhận cập nhật từ gốc (chưa được người duyệt riêng)
_FOLLOWING = {DocStatus.uploaded, DocStatus.processing, DocStatus.needs_review, DocStatus.auto_approved}


async def find_original(
    session: AsyncSession, tenant_id: uuid.UUID, sandbox: bool, sha256: str
) -> Document | None:
    """Document gốc gần nhất cùng nội dung; bỏ qua bản lỗi (cho phép gửi lại) và bản chờ /start nội bộ."""
    stmt = (
        select(Document)
        .where(
            Document.tenant_id == tenant_id,
            Document.sandbox == sandbox,
            Document.sha256 == sha256,
            Document.duplicate_of.is_(None),
            Document.status != DocStatus.failed,
            # upload nội bộ chờ bấm "Bắt đầu" có thể không bao giờ chạy; upload qua API luôn chạy ngay
            (Document.status != DocStatus.uploaded) | Document.api_key_id.is_not(None),
        )
        .order_by(Document.created_at.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def copy_result(session: AsyncSession, src: Document, dst: Document) -> bool:
    """Chép kết quả hiện tại của gốc sang bản trùng (chưa commit). False nếu gốc chưa xong."""
    if src.status not in _READY:
        return False
    dst.pages, dst.pdf_type, dst.loai_van_ban = src.pages, src.pdf_type, src.loai_van_ban
    dst.text_refs = list(src.text_refs or [])
    if src.status == DocStatus.failed:
        dst.status, dst.error = DocStatus.failed, src.error
        return True
    ext = await latest_extraction(session, src.id)
    if ext is None:
        return False
    mine = await latest_extraction(session, dst.id)
    if mine is not None and mine.data == ext.data and dst.status == src.status:
        return False  # đã khớp gốc
    prev = mine.version if mine else 0
    session.add(
        Extraction(
            document_id=dst.id,
            version=prev + 1,
            data=copy.deepcopy(ext.data),
            schema_version=SCHEMA_VERSION,
            provider=ext.provider,
            model=ext.model,
            needs_review=ext.needs_review,
            created_by=f"dedup:{src.id}",
        )
    )
    dst.status, dst.reviewed_by, dst.error = src.status, src.reviewed_by, None
    sync_summary(dst, ext.data)
    await session.flush()
    return True


async def propagate(session: AsyncSession, src: Document) -> int:
    """Cập nhật mọi bản trùng còn theo gốc + tạo sự kiện webhook (chưa commit)."""
    dups = list(
        await session.scalars(
            select(Document).where(Document.duplicate_of == src.id, Document.status.in_(_FOLLOWING))
        )
    )
    n = 0
    for d in dups:
        if await copy_result(session, src, d):
            await webhooks.sync_document(session, d)
            n += 1
    return n
