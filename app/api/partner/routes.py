"""API đối tác /v1 (xác thực `Authorization: Bearer <api_key>`). Tài liệu: docs/PARTNER_API.md, /v1/docs.

Cách ly tenant: mọi truy vấn lọc theo tenant của key VÀ chế độ của key (live/sandbox); tài nguyên
không thuộc phạm vi đó luôn trả 404 (không tiết lộ là có tồn tại).
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.partner import idempotency
from app.api.partner.deps import AnyKeyDep, Partner, ReadDep, SessionDep, SettingsDep, WriteDep
from app.api.partner.errors import ApiError
from app.core.config import Settings
from app.db.models import Batch, DocStatus, Document, WebhookDelivery
from app.models.schema import SCHEMA_VERSION, GiayPhep, public_json_schema
from app.pipeline.export import to_xlsx
from app.pipeline.pdf import PdfError, inspect_pdf
from app.services import duplicates, partner, webhooks
from app.services.documents import ApiFile, create_api_documents
from app.services.estimate import estimate
from app.services.exporting import build_zip
from app.services.queries import latest_extraction
from app.services.storage import UploadError, is_pdf, read_zip

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["partner"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
Scenario = Literal["completed", "failed", "rejected"]
IdemHeader = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key", description="Khóa duy nhất do đối tác sinh (vd UUID); gửi lại = cùng kết quả"
    ),
]


# ---------------- Response models (cho OpenAPI) ----------------


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: Any | None = None


class ErrorOut(BaseModel):
    error: ErrorBody


class DocumentError(BaseModel):
    code: str
    message: str


class DocumentOut(BaseModel):
    id: str
    object: Literal["document"] = "document"
    external_id: str | None
    batch_id: str | None
    file_name: str
    folder_name: str | None
    sha256: str
    status: partner.PublicStatus
    needs_review: bool | None = Field(
        description="Chỉ có khi status=completed mà chưa qua người duyệt (tenant require_human_review=false)"
    )
    document_type: str | None
    pages: int | None
    pdf_type: str | None
    sandbox: bool
    deduplicated: bool = Field(description="true: cùng file đã gửi trước đó, kết quả được dùng lại")
    created_at: str | None
    updated_at: str | None
    error: DocumentError | None
    result_version: int | None
    result: dict[str, Any] | None = Field(
        default=None, description="Kết quả theo GET /v1/schema; chỉ có khi status=completed"
    )


class UploadOut(BaseModel):
    object: Literal["upload"] = "upload"
    batch_id: str
    documents: list[DocumentOut]


class BatchOut(BaseModel):
    id: str
    object: Literal["batch"] = "batch"
    name: str | None
    status: Literal["processing", "completed"]
    sandbox: bool
    created_at: str | None
    counts: dict[str, int]
    documents: list[DocumentOut]


class DocumentList(BaseModel):
    object: Literal["list"] = "list"
    data: list[DocumentOut]


class DeliveryOut(BaseModel):
    id: str
    event: str
    url: str
    status: str
    attempts: int
    last_status_code: int | None
    last_error: str | None
    created_at: str | None
    next_attempt_at: str | None
    delivered_at: str | None


class UsageLimits(BaseModel):
    monthly_page_quota: int | None
    pages_remaining: int | None
    monthly_budget_vnd: float | None
    budget_remaining_vnd: float | None
    rate_limit_per_minute: int


class UsageOut(BaseModel):
    object: Literal["usage"] = "usage"
    period: str
    mode: Literal["live", "sandbox"]
    documents: int
    pages: int
    cost_vnd: float
    llm_calls: int
    limits: UsageLimits


class ResendIn(BaseModel):
    webhook_url: str | None = Field(default=None, description="Gửi tới URL khác (và lưu làm URL mới)")


ERRORS: dict[int | str, dict[str, Any]] = {
    s: {"model": ErrorOut} for s in (401, 403, 404, 409, 413, 415, 422, 429)
}


# ---------------- Helpers ----------------


async def _get_doc(session: AsyncSession, p: Partner, doc_id: uuid.UUID) -> Document:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.tenant_id != p.tenant.id or doc.sandbox != p.sandbox:
        raise ApiError("not_found", "Không tìm thấy document")
    return doc


async def _get_batch(session: AsyncSession, p: Partner, batch_id: uuid.UUID) -> Batch:
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.tenant_id != p.tenant.id or batch.sandbox != p.sandbox:
        raise ApiError("not_found", "Không tìm thấy batch")
    return batch


async def _view(session: AsyncSession, p: Partner, doc: Document, *, result: bool = True) -> dict[str, Any]:
    ext = await latest_extraction(session, doc.id)
    return partner.document_view(doc, p.tenant.require_human_review, ext, include_result=result)


async def _read(f: UploadFile, max_bytes: int, what: str) -> bytes:
    data = await f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ApiError("file_too_large", f"{f.filename}: vượt {max_bytes // (1024 * 1024)}MB ({what})")
    return data


async def _check_pdf(settings: Settings, name: str, data: bytes) -> tuple[int, int]:
    if not is_pdf(data):
        raise ApiError("unsupported_media_type", f"{name}: không phải PDF")
    try:
        pages, text_pages = await asyncio.to_thread(inspect_pdf, data)
    except PdfError as e:
        raise ApiError("invalid_pdf", f"{name}: {e.code}") from None
    if pages > settings.max_pages:
        raise ApiError("too_many_pages", f"{name}: {pages} trang, tối đa {settings.max_pages}")
    return pages, text_pages


def _check_webhook(url: str | None, settings: Settings) -> None:
    if url and (err := webhooks.validate_url(url, settings)):
        raise ApiError("invalid_webhook_url", err)


def _check_scenario(p: Partner, scenario: str | None) -> None:
    if scenario and not p.sandbox:
        raise ApiError("invalid_request", "sandbox_scenario chỉ dùng với key ds_test_")


async def _check_quota(session: AsyncSession, settings: Settings, p: Partner, files: list[ApiFile]) -> None:
    if p.sandbox:
        return  # sandbox miễn phí, không tính hạn mức
    new = [f for f in files if await duplicates.find_original(session, p.tenant.id, False, f.sha256) is None]
    used = await partner.usage(session, p.tenant.id, False)
    est = estimate(settings, [(f.pages, _kind(f)) for f in new])
    if err := partner.check_quota(p.tenant, used, sum(f.pages for f in new), est.cost_vnd):
        raise ApiError(err.code, err.message)


def _kind(f: ApiFile) -> str:
    return "TEXT" if f.text_pages == f.pages else "SCAN"


async def _with_idempotency(
    session: AsyncSession,
    settings: Settings,
    p: Partner,
    key: str | None,
    fp: str,
    run: Callable[[], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    if key is None:
        return JSONResponse(await run(), status_code=202)
    row = await idempotency.begin(session, p, key, fp, settings.idempotency_ttl_hours)
    if isinstance(row, idempotency.Replay):
        return JSONResponse(
            row.body, status_code=row.status_code, headers={idempotency.REPLAY_HEADER: "true"}
        )
    try:
        body = await run()
    except BaseException:
        await idempotency.abort(session, row)
        raise
    await idempotency.finish(session, row, 202, body)
    return JSONResponse(body, status_code=202)


async def _accept(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    p: Partner,
    files: list[ApiFile],
    *,
    batch: Batch | None,
    batch_name: str | None,
    source: str,
    webhook_url: str | None,
    scenario: str | None,
) -> dict[str, Any]:
    await _check_quota(session, settings, p, files)
    if batch is None:
        batch = Batch(
            tenant_id=p.tenant.id,
            name=batch_name,
            source=source,
            created_by=f"api_key:{p.key.prefix}",
            sandbox=p.sandbox,
            webhook_url=webhook_url if source == "api_zip" else None,
        )
        session.add(batch)
        await session.flush()
    docs, to_run = await create_api_documents(
        session, settings, p.key, batch, files, webhook_url=webhook_url, sandbox_scenario=scenario
    )
    try:
        for d in to_run:
            await request.app.state.enqueue(d.id)
    except Exception as e:  # Redis/arq lỗi: đánh dấu failed để lần gửi lại không bị coi là bản trùng
        log.error("enqueue lỗi", extra={"error": type(e).__name__, "batch": str(batch.id)})
        for d in to_run:
            d.status, d.error = DocStatus.failed, "EnqueueError: hàng đợi không sẵn sàng"
        await session.commit()
        raise ApiError("service_unavailable") from None
    log.info(
        "api upload",
        extra={"batch": str(batch.id), "documents": len(docs), "queued": len(to_run), "sandbox": p.sandbox},
    )
    views = [await _view(session, p, d) for d in docs]
    return {"object": "upload", "batch_id": str(batch.id), "documents": views}


# ---------------- Upload ----------------


@router.post(
    "/documents",
    status_code=202,
    response_model=UploadOut,
    responses=ERRORS,
    summary="Gửi 1..N file PDF",
    description=(
        "Nhận ngay (202), xử lý bất đồng bộ. Theo dõi bằng webhook hoặc GET /v1/documents/{id}. "
        "Mỗi file ≤ MAX_FILE_MB (20MB), ≤ MAX_PAGES (30) trang. Cùng file (sha256) đã gửi trước đó -> "
        "không xử lý lại, dùng lại kết quả (deduplicated=true)."
    ),
)
async def upload_documents(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    p: WriteDep,
    files: Annotated[list[UploadFile], File(description="1..N file PDF")],
    external_id: Annotated[
        list[str] | None,
        Form(description="Mã của đối tác; lặp lại theo đúng thứ tự file (1 giá trị / file)", max_length=255),
    ] = None,
    batch_id: Annotated[uuid.UUID | None, Form(description="Thêm vào batch đã có (của chính tenant)")] = None,
    webhook_url: Annotated[str | None, Form(description="URL https nhận sự kiện trạng thái cuối")] = None,
    folder_name: Annotated[str | None, Form(max_length=512, description="Nhóm hồ sơ (vd tên báo)")] = None,
    sandbox_scenario: Annotated[
        Scenario | None, Form(description="Chỉ key ds_test_: mô phỏng kết quả")
    ] = None,
    idempotency_key: IdemHeader = None,
) -> JSONResponse:
    if not 1 <= len(files) <= settings.api_max_files_per_request:
        raise ApiError("too_many_files", f"Gửi 1..{settings.api_max_files_per_request} file / request")
    ext_ids = external_id or []
    if ext_ids and len(ext_ids) != len(files):
        raise ApiError(
            "invalid_request", f"external_id: cần {len(files)} giá trị (1 / file), nhận {len(ext_ids)}"
        )
    _check_webhook(webhook_url, settings)
    _check_scenario(p, sandbox_scenario)
    batch = await _get_batch(session, p, batch_id) if batch_id else None

    items: list[ApiFile] = []
    total, max_total = 0, settings.max_upload_mb * 1024 * 1024
    for i, f in enumerate(files):
        name = f.filename or f"document_{i + 1}.pdf"
        data = await _read(f, settings.max_file_mb * 1024 * 1024, "MAX_FILE_MB")
        total += len(data)
        if total > max_total:
            raise ApiError("file_too_large", f"Tổng dung lượng vượt {settings.max_upload_mb}MB / request")
        pages, text_pages = await _check_pdf(settings, name, data)
        items.append(ApiFile(folder_name, name, data, pages, text_pages, ext_ids[i] if ext_ids else None))

    params = {
        "external_id": ext_ids,
        "batch_id": batch_id,
        "webhook_url": webhook_url,
        "folder_name": folder_name,
        "sandbox_scenario": sandbox_scenario,
    }
    fp = idempotency.fingerprint("POST /v1/documents", params, [(f.file_name, f.sha256) for f in items])

    async def run() -> dict[str, Any]:
        return await _accept(
            request, session, settings, p, items,
            batch=batch, batch_name=None, source="api", webhook_url=webhook_url, scenario=sandbox_scenario,
        )  # fmt: skip

    return await _with_idempotency(session, settings, p, idempotency_key, fp, run)


@router.post(
    "/batches",
    status_code=202,
    response_model=UploadOut,
    responses=ERRORS,
    summary="Gửi 1 file ZIP (thư mục hồ sơ)",
    description=(
        "Mỗi PDF trong ZIP -> 1 document; folder_name = thư mục chứa PDF. ZIP ≤ MAX_UPLOAD_MB (100MB), "
        "≤ 500 file. webhook_url nhận sự kiện từng document và `batch.completed` khi cả batch xong."
    ),
)
async def upload_zip(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    p: WriteDep,
    file: Annotated[UploadFile, File(description="File ZIP")],
    name: Annotated[
        str | None, Form(max_length=255, description="Tên batch (mặc định: tên file ZIP)")
    ] = None,
    webhook_url: Annotated[str | None, Form()] = None,
    sandbox_scenario: Annotated[Scenario | None, Form()] = None,
    idempotency_key: IdemHeader = None,
) -> JSONResponse:
    _check_webhook(webhook_url, settings)
    _check_scenario(p, sandbox_scenario)
    data = await _read(file, settings.max_upload_mb * 1024 * 1024, "MAX_UPLOAD_MB")
    try:
        incoming = read_zip(data, max_file_bytes=settings.max_file_mb * 1024 * 1024)
    except UploadError as e:
        raise ApiError("invalid_zip", str(e)) from None
    items: list[ApiFile] = []
    for f in incoming:
        label = f"{f.folder_name}/{f.file_name}" if f.folder_name else f.file_name
        pages, text_pages = await _check_pdf(settings, label, f.data)
        items.append(ApiFile(f.folder_name, f.file_name, f.data, pages, text_pages))
    params = {"name": name, "webhook_url": webhook_url, "sandbox_scenario": sandbox_scenario}
    files_fp = [(f"{f.folder_name}/{f.file_name}", f.sha256) for f in items]
    fp = idempotency.fingerprint("POST /v1/batches", params, files_fp)

    async def run() -> dict[str, Any]:
        return await _accept(
            request, session, settings, p, items,
            batch=None, batch_name=name or file.filename, source="api_zip",
            webhook_url=webhook_url, scenario=sandbox_scenario,
        )  # fmt: skip

    return await _with_idempotency(session, settings, p, idempotency_key, fp, run)


# ---------------- Đọc ----------------


@router.get("/documents", response_model=DocumentList, responses=ERRORS, summary="Danh sách document")
async def list_documents(
    session: SessionDep,
    p: ReadDep,
    external_id: str | None = None,
    batch_id: uuid.UUID | None = None,
    status: partner.PublicStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    stmt = select(Document).where(Document.tenant_id == p.tenant.id, Document.sandbox == p.sandbox)
    if external_id:
        stmt = stmt.where(Document.external_id == external_id)
    if batch_id:
        stmt = stmt.where(Document.batch_id == batch_id)
    docs = list(
        await session.scalars(stmt.order_by(Document.created_at.desc()).limit(limit if not status else 1000))
    )
    views = [await _view(session, p, d, result=False) for d in docs]
    if status:
        views = [v for v in views if v["status"] == status.value][:limit]
    return {"object": "list", "data": views}


@router.get(
    "/documents/{doc_id}",
    response_model=DocumentOut,
    responses=ERRORS,
    summary="Trạng thái + kết quả JSON",
)
async def get_document(session: SessionDep, p: ReadDep, doc_id: uuid.UUID) -> dict[str, Any]:
    return await _view(session, p, await _get_doc(session, p, doc_id))


async def _released(session: AsyncSession, p: Partner, doc: Document) -> GiayPhep:
    ext = await latest_extraction(session, doc.id)
    st = partner.public_status(doc, p.tenant.require_human_review)
    if st != partner.PublicStatus.completed or ext is None:
        raise ApiError("not_ready", f"Document đang ở trạng thái {st.value}, chưa có kết quả")
    return GiayPhep.model_validate(partner.public_result(ext.data))


@router.get(
    "/documents/{doc_id}/export.xlsx",
    responses={200: {"content": {XLSX: {}}}, **ERRORS},
    response_class=Response,
    summary="Tải kết quả Excel",
)
async def export_document(session: SessionDep, p: ReadDep, doc_id: uuid.UUID) -> Response:
    doc = await _get_doc(session, p, doc_id)
    gp = await _released(session, p, doc)
    stem = doc.file_name.rsplit(".", 1)[0]
    return Response(
        to_xlsx(gp, folder=doc.folder_name),
        media_type=XLSX,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(stem)}.xlsx"},
    )


@router.get("/batches/{batch_id}", response_model=BatchOut, responses=ERRORS, summary="Trạng thái batch")
async def get_batch(session: SessionDep, p: ReadDep, batch_id: uuid.UUID) -> dict[str, Any]:
    batch = await _get_batch(session, p, batch_id)
    docs = list(await session.scalars(select(Document).where(Document.batch_id == batch.id)))
    return partner.batch_view(batch, [await _view(session, p, d, result=False) for d in docs])


@router.get(
    "/batches/{batch_id}/export.zip",
    responses={200: {"content": {"application/zip": {}}}, **ERRORS},
    response_class=Response,
    summary="Tải ZIP kết quả của batch",
    description="Chỉ gồm các document completed: <thư mục>/<tên>.xlsx + TongHop.xlsx.",
)
async def export_batch(session: SessionDep, p: ReadDep, batch_id: uuid.UUID) -> Response:
    batch = await _get_batch(session, p, batch_id)
    docs = await session.scalars(
        select(Document)
        .where(Document.batch_id == batch.id)
        .order_by(Document.folder_name, Document.file_name)
    )
    items = []
    for d in docs:
        ext = await latest_extraction(session, d.id)
        if ext is not None and partner.public_status(d, p.tenant.require_human_review) == "completed":
            items.append((d, ext))
    if not items:
        raise ApiError("not_ready", "Batch chưa có document nào completed")
    return Response(
        build_zip(items, for_partner=True),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="batch_{batch.id}.zip"'},
    )


@router.get("/schema", summary="JSON Schema của kết quả", responses=ERRORS)
async def get_schema(p: AnyKeyDep) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "api_version": __version__,
        "statuses": [s.value for s in partner.PublicStatus],
        "json_schema": public_json_schema(),
    }


@router.get("/usage", response_model=UsageOut, responses=ERRORS, summary="Số trang và chi phí trong kỳ")
async def get_usage(
    session: SessionDep,
    settings: SettingsDep,
    p: ReadDep,
    period: Annotated[
        str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM (giờ VN)")
    ] = None,
) -> dict[str, Any]:
    u = await partner.usage(session, p.tenant.id, p.sandbox, period)
    t = p.tenant
    quota = None if p.sandbox else t.monthly_page_quota
    budget = None if p.sandbox else t.monthly_budget_vnd
    return {
        "object": "usage",
        "period": u.period,
        "mode": "sandbox" if p.sandbox else "live",
        "documents": u.documents,
        "pages": u.pages,
        "cost_vnd": u.cost_vnd,
        "llm_calls": u.llm_calls,
        "limits": {
            "monthly_page_quota": quota,
            "pages_remaining": None if quota is None else max(0, quota - u.pages),
            "monthly_budget_vnd": budget,
            "budget_remaining_vnd": None if budget is None else max(0.0, round(budget - u.cost_vnd, 1)),
            "rate_limit_per_minute": partner.rate_limit(t, settings),
        },
    }


# ---------------- Webhook ----------------


def _delivery(d: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "event": d.event,
        "url": d.url,
        "status": d.status,
        "attempts": d.attempts,
        "last_status_code": d.last_status_code,
        "last_error": d.last_error,
        "created_at": partner.iso(d.created_at),
        "next_attempt_at": partner.iso(d.next_attempt_at),
        "delivered_at": partner.iso(d.delivered_at),
    }


@router.get(
    "/documents/{doc_id}/webhooks",
    response_model=list[DeliveryOut],
    responses=ERRORS,
    summary="Lịch sử gửi webhook",
)
async def list_deliveries(session: SessionDep, p: ReadDep, doc_id: uuid.UUID) -> list[dict[str, Any]]:
    doc = await _get_doc(session, p, doc_id)
    rows = await session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.document_id == doc.id, WebhookDelivery.tenant_id == p.tenant.id)
        .order_by(WebhookDelivery.created_at.desc())
    )
    return [_delivery(d) for d in rows]


@router.post(
    "/documents/{doc_id}/webhook/resend",
    status_code=202,
    response_model=DeliveryOut,
    responses=ERRORS,
    summary="Gửi lại webhook trạng thái cuối",
)
async def resend_webhook(
    session: SessionDep,
    settings: SettingsDep,
    p: WriteDep,
    doc_id: uuid.UUID,
    body: Annotated[ResendIn | None, Body()] = None,
) -> dict[str, Any]:
    doc = await _get_doc(session, p, doc_id)
    url = body.webhook_url if body else None
    _check_webhook(url, settings)
    if not (url or doc.webhook_url):
        raise ApiError("invalid_request", "Document không có webhook_url; truyền webhook_url trong body")
    if url:
        doc.webhook_url = url
    d = await webhooks.resend(session, doc, url)
    if d is None:
        raise ApiError("not_final")
    await session.commit()
    return _delivery(d)
