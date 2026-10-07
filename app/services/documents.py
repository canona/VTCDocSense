"""Nghiệp vụ document: tạo từ upload, xử lý trong worker, đối chiếu chéo, ghi llm_calls."""

import asyncio
import hashlib
import logging
import uuid
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import Settings
from app.db.models import (
    FINAL_REVIEW_STATUSES,
    Batch,
    DocStatus,
    Document,
    Extraction,
    LlmCall,
)
from app.llm.metered import MeteredProvider, RunState
from app.models.schema import SCHEMA_VERSION, GiayPhep, LoaiVanBan
from app.pipeline.pdf import PdfError
from app.pipeline.run import all_critical_high, process_pdf_full
from app.pipeline.verify import CrossDoc, GpRef, crosscheck, refs_from_gp
from app.providers import ExtractionProvider, ProviderError
from app.services.storage import IncomingPdf, save_pdf

log = logging.getLogger(__name__)


def decide_status(gp: GiayPhep) -> DocStatus:
    """auto_approved chỉ khi là GP và mọi trường trọng yếu có giá trị, confidence=high."""
    if gp.loai_van_ban != LoaiVanBan.KHAC and all_critical_high(gp):
        return DocStatus.auto_approved
    return DocStatus.needs_review


async def create_documents(
    session: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    files: list[IncomingPdf],
    *,
    source: str,
    batch_name: str | None = None,
    created_by: str | None = None,
) -> tuple[Batch, list[Document]]:
    batch = Batch(tenant_id=tenant_id, name=batch_name, source=source, created_by=created_by)
    session.add(batch)
    await session.flush()
    docs: list[Document] = []
    for f in files:
        doc_id = uuid.uuid4()
        path = await asyncio.to_thread(save_pdf, settings.upload_dir, tenant_id, doc_id, f.data)
        doc = Document(
            id=doc_id,
            tenant_id=tenant_id,
            batch_id=batch.id,
            folder_name=f.folder_name,
            file_name=f.file_name,
            sha256=hashlib.sha256(f.data).hexdigest(),
            size_bytes=len(f.data),
            storage_path=str(path),
            status=DocStatus.uploaded,
            job_id=f"doc:{doc_id}",
        )
        session.add(doc)
        docs.append(doc)
    await session.commit()
    return batch, docs


def _save_calls(session: AsyncSession, doc: Document, run: RunState) -> None:
    for r in run.records:
        session.add(
            LlmCall(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                provider=r.provider,
                model=r.model,
                schema_name=r.schema_name,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cached_input_tokens=r.cached_input_tokens,
                cost_usd=r.cost_usd,
                cache_hit=r.cache_hit,
                live=r.live,
                duration_ms=r.duration_ms,
                error=r.error,
            )
        )


async def latest_extraction(session: AsyncSession, doc_id: uuid.UUID) -> Extraction | None:
    stmt = (
        select(Extraction)
        .where(Extraction.document_id == doc_id)
        .order_by(Extraction.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def process_document(
    session: AsyncSession,
    settings: Settings,
    doc_id: uuid.UUID,
    provider: ExtractionProvider,
    fallback: ExtractionProvider | None = None,
) -> DocStatus | None:
    doc = await session.get(Document, doc_id)
    if doc is None:
        log.warning("document không tồn tại", extra={"doc": str(doc_id)})
        return None
    if doc.status in FINAL_REVIEW_STATUSES:
        return DocStatus(doc.status)
    doc.status, doc.error = DocStatus.processing, None
    await session.commit()

    run = RunState()  # LLM_MAX_CALLS_PER_RUN tính theo từng document
    primary = MeteredProvider(provider, settings, run=run)
    fb = MeteredProvider(fallback, settings, run=run) if fallback else None
    try:
        data = await asyncio.to_thread(Path(doc.storage_path).read_bytes)
        out = await process_pdf_full(data, doc.file_name, primary, settings, fb)
    except (PdfError, ProviderError, OSError) as e:
        doc.status, doc.error = DocStatus.failed, f"{type(e).__name__}: {e}"[:2000]
        _save_calls(session, doc, run)
        await session.commit()
        log.warning("xử lý document lỗi", extra={"doc": str(doc.id), "error": doc.error})
        return DocStatus.failed

    gp = out.gp
    prev = await session.scalar(select(func.max(Extraction.version)).where(Extraction.document_id == doc.id))
    session.add(
        Extraction(
            document_id=doc.id,
            version=(prev or 0) + 1,
            data=gp.model_dump(mode="json"),
            schema_version=SCHEMA_VERSION,
            provider=gp.meta.provider,
            model=gp.meta.model,
            input_tokens=gp.meta.input_tokens,
            output_tokens=gp.meta.output_tokens,
            cost_usd=run.cost_usd,
            needs_review=gp.needs_review,
        )
    )
    doc.pages, doc.pdf_type, doc.loai_van_ban = gp.meta.pages, gp.meta.pdf_type.value, gp.loai_van_ban.value
    doc.text_refs = [{"so_gp": r.so_gp, "ngay": r.ngay.isoformat()} for r in out.text_refs]
    doc.status = decide_status(gp)
    _save_calls(session, doc, run)
    await session.commit()
    if doc.folder_name:
        await crosscheck_folder(session, doc.tenant_id, doc.folder_name)
        await session.refresh(doc)
    log.info("document xong", extra={"doc": str(doc.id), "status": doc.status, "cost_usd": run.cost_usd})
    return DocStatus(doc.status)


async def crosscheck_folder(session: AsyncSession, tenant_id: uuid.UUID, folder_name: str) -> int:
    """Đối chiếu số/ngày GP giữa các document cùng thư mục báo; cập nhật bản trích xuất mới nhất."""
    docs = (
        await session.scalars(
            select(Document).where(Document.tenant_id == tenant_id, Document.folder_name == folder_name)
        )
    ).all()
    items: list[tuple[Document, Extraction, CrossDoc]] = []
    for d in docs:
        ext = await latest_extraction(session, d.id)
        if ext is None:
            continue
        gp = GiayPhep.model_validate(ext.data)
        refs = refs_from_gp(gp) + [GpRef(r["so_gp"], date.fromisoformat(r["ngay"])) for r in d.text_refs]
        items.append((d, ext, CrossDoc(str(d.id), d.file_name, gp, list(dict.fromkeys(refs)))))
    changed = crosscheck([c for _, _, c in items])
    for d, ext, c in items:
        if c.key not in changed:
            continue
        ext.data = c.gp.model_dump(mode="json")
        flag_modified(ext, "data")
        if d.status in (DocStatus.needs_review, DocStatus.auto_approved):
            d.status = decide_status(c.gp)
    if changed:
        await session.commit()
        log.info("đối chiếu chéo", extra={"folder": folder_name, "changed": len(changed)})
    return len(changed)
