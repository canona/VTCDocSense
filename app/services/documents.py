"""Nghiệp vụ document: tạo từ upload, xử lý trong worker, đối chiếu chéo, ghi llm_calls."""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import Settings
from app.db.models import (
    FINAL_REVIEW_STATUSES,
    ApiKey,
    Batch,
    DocStatus,
    Document,
    Extraction,
    LlmCall,
)
from app.llm.metered import MeteredProvider, RunState
from app.models.schema import SCHEMA_VERSION, GiayPhep, LoaiVanBan
from app.pipeline.pdf import PdfError, inspect_pdf
from app.pipeline.run import all_critical_high, process_pdf_full
from app.pipeline.verify import CrossDoc, GpRef, crosscheck, refs_from_gp
from app.providers import ExtractionProvider, ProviderError
from app.services import duplicates, webhooks
from app.services.queries import latest_extraction
from app.services.review import sync_summary
from app.services.sandbox import process_sandbox
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
        try:
            pages, text_pages = await asyncio.to_thread(inspect_pdf, f.data)
        except PdfError:
            pages, text_pages = None, 0
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
            pages=pages,
            pdf_type=None if pages is None else _kind(pages, text_pages),
        )
        session.add(doc)
        docs.append(doc)
    await session.commit()
    return batch, docs


@dataclass
class ApiFile:
    """File PDF đã kiểm tra (định dạng, dung lượng, số trang) ở tầng API."""

    folder_name: str | None
    file_name: str
    data: bytes
    pages: int
    text_pages: int
    external_id: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


async def create_api_documents(
    session: AsyncSession,
    settings: Settings,
    key: ApiKey,
    batch: Batch,
    files: list[ApiFile],
    *,
    webhook_url: str | None,
    sandbox_scenario: str | None = None,
) -> tuple[list[Document], list[Document]]:
    """Tạo document từ API đối tác (đã commit). Trả (mọi document, document cần xếp hàng xử lý).

    File trùng (cùng sha256 trong tenant) không được xếp hàng: chép kết quả của bản gốc.
    """
    docs: list[Document] = []
    to_run: list[Document] = []
    for f in files:
        doc_id, sha = uuid.uuid4(), f.sha256
        original = await duplicates.find_original(session, key.tenant_id, key.sandbox, sha)
        path = await asyncio.to_thread(save_pdf, settings.upload_dir, key.tenant_id, doc_id, f.data)
        doc = Document(
            id=doc_id,
            tenant_id=key.tenant_id,
            batch_id=batch.id,
            folder_name=f.folder_name,
            file_name=f.file_name,
            sha256=sha,
            size_bytes=len(f.data),
            storage_path=str(path),
            external_id=f.external_id,
            status=DocStatus.uploaded,
            job_id=f"doc:{doc_id}",
            pages=f.pages,
            pdf_type=_kind(f.pages, f.text_pages),
            api_key_id=key.id,
            sandbox=key.sandbox,
            sandbox_scenario=sandbox_scenario,
            webhook_url=webhook_url,
            duplicate_of=original.id if original else None,
        )
        session.add(doc)
        await session.flush()
        docs.append(doc)
        if original is None:
            to_run.append(doc)
        elif await duplicates.copy_result(session, original, doc):
            await webhooks.sync_document(session, doc)
        else:
            doc.status = DocStatus.processing  # gốc đang xử lý: nhận kết quả khi gốc xong
    await session.commit()
    return docs, to_run


def _kind(pages: int, text_pages: int) -> str:
    return "TEXT" if text_pages == pages else "SCAN" if text_pages == 0 else "MIXED"


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
                cost_vnd=r.cost_vnd,
                cache_hit=r.cache_hit,
                live=r.live,
                duration_ms=r.duration_ms,
                error=r.error,
            )
        )


async def process_document(
    session: AsyncSession,
    settings: Settings,
    doc_id: uuid.UUID,
    provider: ExtractionProvider,
    fallback: ExtractionProvider | None = None,
    *,
    read_cache: bool = True,
) -> DocStatus | None:
    doc = await session.get(Document, doc_id)
    if doc is None:
        log.warning("document không tồn tại", extra={"doc": str(doc_id)})
        return None
    if doc.status in FINAL_REVIEW_STATUSES:
        return DocStatus(doc.status)
    if doc.duplicate_of is not None:  # bản trùng không bao giờ tự xử lý (chờ chép từ gốc)
        return DocStatus(doc.status)
    doc.status, doc.error = DocStatus.processing, None
    await session.commit()
    if doc.sandbox:  # key ds_test_: kết quả mẫu, tuyệt đối không chạm provider
        st = await process_sandbox(session, doc)
        await _finish(session, doc)
        log.info("document sandbox xong", extra={"doc": str(doc.id), "status": st})
        return st

    run = RunState()  # LLM_MAX_CALLS_PER_RUN tính theo từng document
    primary = MeteredProvider(provider, settings, run=run, read_cache=read_cache)
    fb = MeteredProvider(fallback, settings, run=run, read_cache=read_cache) if fallback else None
    try:
        data = await asyncio.to_thread(Path(doc.storage_path).read_bytes)
        out = await process_pdf_full(data, doc.file_name, primary, settings, fb)
    except (PdfError, ProviderError, OSError) as e:
        doc.status, doc.error = DocStatus.failed, f"{type(e).__name__}: {e}"[:2000]
        _save_calls(session, doc, run)
        await _finish(session, doc)
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
            cost_vnd=run.cost_vnd,
            needs_review=gp.needs_review,
        )
    )
    doc.pages, doc.pdf_type, doc.loai_van_ban = gp.meta.pages, gp.meta.pdf_type.value, gp.loai_van_ban.value
    doc.text_refs = [{"so_gp": r.so_gp, "ngay": r.ngay.isoformat()} for r in out.text_refs]
    doc.status = decide_status(gp)
    sync_summary(doc, gp.model_dump(mode="json"))
    _save_calls(session, doc, run)
    await session.commit()
    if doc.folder_name:
        await crosscheck_folder(session, doc.tenant_id, doc.folder_name)
        await session.refresh(doc)
    await _finish(session, doc)
    log.info("document xong", extra={"doc": str(doc.id), "status": doc.status, "cost_vnd": run.cost_vnd})
    return DocStatus(doc.status)


async def _finish(session: AsyncSession, doc: Document) -> None:
    """Sau khi document có trạng thái mới: sự kiện webhook + chép kết quả sang các bản trùng."""
    await webhooks.sync_document(session, doc)
    await duplicates.propagate(session, doc)
    await session.commit()


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
        sync_summary(d, ext.data)
        if d.status in (DocStatus.needs_review, DocStatus.auto_approved):
            d.status = decide_status(c.gp)
        await webhooks.sync_document(session, d)  # kết quả đã báo bị đối chiếu sửa -> document.updated
    if changed:
        await session.commit()
        log.info("đối chiếu chéo", extra={"folder": folder_name, "changed": len(changed)})
    return len(changed)
