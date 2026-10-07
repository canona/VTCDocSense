"""API nội bộ (/api): upload -> job_id ngay, xem trạng thái, llm_calls.

Chưa có đăng nhập (M3: Cloudflare Access); tạm bảo vệ bằng INTERNAL_API_TOKEN, hoặc chỉ mở khi APP_ENV=local.
Mọi upload thuộc tenant mặc định.
"""

import hmac
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import DEFAULT_TENANT_ID, Batch, DocStatus, Document, LlmCall
from app.db.session import get_sessionmaker
from app.models.schema import GiayPhep
from app.pipeline.export import to_xlsx
from app.services.documents import create_documents
from app.services.estimate import estimate
from app.services.queries import latest_extraction
from app.services.storage import IncomingPdf, UploadError, is_pdf, read_zip


def require_internal(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    token = settings.internal_api_token
    if token is not None:
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), token.get_secret_value().encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sai hoặc thiếu INTERNAL_API_TOKEN")
    elif settings.app_env != "local":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "API nội bộ chỉ mở khi APP_ENV=local hoặc có token")


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


router = APIRouter(prefix="/api", tags=["internal"], dependencies=[Depends(require_internal)])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _read_limited(f: UploadFile, max_bytes: int) -> bytes:
    data = await f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"{f.filename}: vượt {max_bytes // (1024 * 1024)}MB"
        )
    return data


def _doc_brief(d: Document) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "job_id": d.job_id,
        "file_name": d.file_name,
        "folder_name": d.folder_name,
        "status": d.status,
        "loai_van_ban": d.loai_van_ban,
        "pdf_type": d.pdf_type,
        "pages": d.pages,
        "error": d.error,
    }


async def _accept(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    files: list[IncomingPdf],
    source: str,
    name: str | None,
    auto_start: bool,
) -> dict[str, Any]:
    batch, docs = await create_documents(
        session, settings, DEFAULT_TENANT_ID, files, source=source, batch_name=name
    )
    if auto_start:
        for d in docs:
            await request.app.state.enqueue(d.id)
    est = estimate(settings, [(d.pages, d.pdf_type) for d in docs])
    return {
        "batch_id": str(batch.id),
        "started": auto_start,
        "estimate": asdict(est),
        "documents": [_doc_brief(d) for d in docs],
    }


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_documents(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    files: Annotated[list[UploadFile], File(description="1..N file PDF")],
    folder_name: Annotated[str | None, Form()] = None,
    auto_start: Annotated[bool, Form(description="true: chạy ngay; mặc định chờ /start")] = False,
) -> dict[str, Any]:
    incoming: list[IncomingPdf] = []
    for f in files:
        data = await _read_limited(f, settings.max_file_mb * 1024 * 1024)
        if not is_pdf(data):
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{f.filename}: không phải PDF")
        incoming.append(IncomingPdf(folder_name, f.filename or "document.pdf", data))
    return await _accept(request, session, settings, incoming, "upload", None, auto_start)


@router.post("/batches", status_code=status.HTTP_202_ACCEPTED)
async def upload_zip(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="ZIP chứa các thư mục báo")],
    auto_start: Annotated[bool, Form(description="true: chạy ngay; mặc định chờ /start")] = False,
) -> dict[str, Any]:
    data = await _read_limited(file, settings.max_upload_mb * 1024 * 1024)
    try:
        incoming = read_zip(data, max_file_bytes=settings.max_file_mb * 1024 * 1024)
    except UploadError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    return await _accept(request, session, settings, incoming, "zip", file.filename, auto_start)


@router.post("/batches/{batch_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_batch(request: Request, session: SessionDep, batch_id: uuid.UUID) -> dict[str, Any]:
    """Xác nhận chạy (tốn phí) các document còn ở trạng thái uploaded."""
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy batch")
    docs = await start_documents(request, session, batch.id)
    return {"batch_id": str(batch.id), "started": len(docs)}


async def start_documents(request: Request, session: AsyncSession, batch_id: uuid.UUID) -> list[Document]:
    docs = list(
        await session.scalars(
            select(Document).where(Document.batch_id == batch_id, Document.status == DocStatus.uploaded)
        )
    )
    for d in docs:
        await request.app.state.enqueue(d.id)
    return docs


async def _get_doc(session: AsyncSession, doc_id: uuid.UUID) -> Document:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy document")
    return doc


@router.get("/documents")
async def list_documents(
    session: SessionDep, status_: Annotated[str | None, Query(alias="status")] = None, limit: int = 100
) -> list[dict[str, Any]]:
    stmt = select(Document).where(Document.tenant_id == DEFAULT_TENANT_ID)
    if status_:
        stmt = stmt.where(Document.status == status_)
    docs = await session.scalars(stmt.order_by(Document.created_at.desc()).limit(min(limit, 500)))
    return [_doc_brief(d) for d in docs]


@router.get("/documents/{doc_id}")
async def get_document(session: SessionDep, doc_id: uuid.UUID) -> dict[str, Any]:
    doc = await _get_doc(session, doc_id)
    ext = await latest_extraction(session, doc.id)
    return {
        **_doc_brief(doc),
        "extraction": None
        if ext is None
        else {"version": ext.version, "model": ext.model, "cost_vnd": ext.cost_vnd, "data": ext.data},
    }


@router.get("/documents/{doc_id}/export.xlsx")
async def export_document(session: SessionDep, doc_id: uuid.UUID) -> Response:
    doc = await _get_doc(session, doc_id)
    ext = await latest_extraction(session, doc.id)
    if ext is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document chưa có kết quả trích xuất")
    body = to_xlsx(GiayPhep.model_validate(ext.data), folder=doc.folder_name)
    stem = doc.file_name.rsplit(".", 1)[0]
    return Response(
        body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stem)}.xlsx"},
    )


@router.get("/batches/{batch_id}")
async def get_batch(session: SessionDep, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy batch")
    docs = (await session.scalars(select(Document).where(Document.batch_id == batch.id))).all()
    counts: dict[str, int] = {}
    for d in docs:
        counts[d.status] = counts.get(d.status, 0) + 1
    return {
        "id": str(batch.id),
        "name": batch.name,
        "created_at": batch.created_at.isoformat(),
        "counts": counts,
        "documents": [_doc_brief(d) for d in docs],
    }


@router.get("/llm-calls")
async def llm_calls(session: SessionDep, limit: int = 50) -> dict[str, Any]:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    row = (
        await session.execute(
            select(
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.cost_vnd), 0.0),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
            ).where(LlmCall.created_at >= today, LlmCall.live.is_(True))
        )
    ).one()
    calls = await session.scalars(select(LlmCall).order_by(LlmCall.created_at.desc()).limit(min(limit, 500)))
    return {
        "today_live": {"calls": row[0], "cost_vnd": row[1], "input_tokens": row[2], "output_tokens": row[3]},
        "calls": [
            {
                "created_at": c.created_at.isoformat(),
                "document_id": str(c.document_id) if c.document_id else None,
                "provider": c.provider,
                "model": c.model,
                "schema": c.schema_name,
                "in": c.input_tokens,
                "out": c.output_tokens,
                "cached": c.cached_input_tokens,
                "cost_vnd": c.cost_vnd,
                "cache_hit": c.cache_hit,
                "live": c.live,
                "ms": c.duration_ms,
                "error": c.error,
            }
            for c in calls
        ],
    }
