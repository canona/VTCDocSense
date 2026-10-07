"""Giao diện web rà soát nội bộ (Jinja2 + HTMX + Alpine.js, không build frontend)."""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.internal import get_session, start_documents
from app.core.config import Settings, get_settings
from app.db.models import (
    DEFAULT_TENANT_ID,
    ApiKey,
    AuditLog,
    Batch,
    DocStatus,
    Document,
    Extraction,
    FieldReview,
    LlmCall,
    Role,
    Tenant,
    User,
)
from app.llm.store import Ledger
from app.models.schema import ChucVu, GiayPhep, LoaiHinh, LoaiVanBan
from app.pipeline.export import CHUC_VU_LABEL, LOAI_HINH_LABEL, LOAI_LABEL, to_xlsx
from app.pipeline.run import CRITICAL_FIELDS
from app.pipeline.validate import FIELD_LABELS
from app.services import duplicates, webhooks
from app.services.api_keys import new_api_key
from app.services.documents import create_documents
from app.services.estimate import estimate
from app.services.exporting import STATUS_LABEL, build_zip
from app.services.queries import latest_extraction
from app.services.review import (
    AN_PHAM_KEYS,
    FIELD_GROUPS,
    LONG_FIELDS,
    apply_review,
    display_value,
    get_path,
)
from app.services.storage import IncomingPdf, UploadError, is_pdf, read_zip
from app.web.auth import DEV_COOKIE, CurrentUser, UserDep, require
from app.web.page_images import page_file, page_labels

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(include_in_schema=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ReviewerDep = Annotated[CurrentUser, Depends(require(Role.reviewer))]
AdminDep = Annotated[CurrentUser, Depends(require(Role.admin))]

CONF_LABEL = {"high": "Cao", "medium": "Trung bình", "low": "Thấp"}
AN_PHAM_LABELS = {
    "loai_hinh": "Loại hình",
    "cap": "Cấp",
    "ten_goi": "Tên gọi",
    "ngon_ngu": "Ngôn ngữ",
    "ky_han": "Kỳ hạn",
    "thoi_gian_phat_hanh": "Thời gian phát hành",
    "khuon_kho": "Khuôn khổ",
    "so_trang": "Số trang",
    "so_luong": "Số lượng",
    "noi_in": "Nơi in",
    "ten_mien": "Tên miền",
    "isp": "ISP",
}


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0]


def _fmt_date(d: Any) -> str:
    if isinstance(d, date):
        return d.strftime("%d/%m/%Y")
    return display_value("ngay_cap", d) if d else ""


def _fmt_vnd(v: float | None) -> str:
    """Kiểu Việt Nam: 1.234 đ; số nhỏ giữ 1 chữ số thập phân (5,2 đ)."""
    if v is None:
        return "—"
    s = f"{v:,.1f}" if abs(v) < 100 and v != int(v) else f"{v:,.0f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".") + " đ"


def _fmt_dt(d: datetime | None) -> str:
    return d.astimezone(UTC).strftime("%d/%m/%Y %H:%M") if d else ""


TEMPLATES.env.filters.update(
    dmy=_fmt_date,
    dt=_fmt_dt,
    status_label=lambda s: STATUS_LABEL.get(s, s),
    loai_label=lambda s: LOAI_LABEL.get(LoaiVanBan(s), s) if s in LoaiVanBan.__members__ else (s or ""),
    vnd=_fmt_vnd,
    num=lambda v: f"{v:,}".replace(",", "."),
)
TEMPLATES.env.globals.update(CONF_LABEL=CONF_LABEL, STATUS_LABEL=STATUS_LABEL, FIELD_LABELS=FIELD_LABELS)


def render(
    request: Request, name: str, user: CurrentUser | None, code: int = 200, **ctx: Any
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, {"user": user, **ctx}, status_code=code)


def _back(url: str, **flash: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (sep + urlencode(flash) if flash else ""), status.HTTP_303_SEE_OTHER)


def _is_staff(user: CurrentUser) -> bool:
    """Người dùng thuộc tenant mặc định = nhân viên VTCDocSense: rà soát cho mọi tenant
    (đối tác API có require_human_review). Người dùng tenant khác chỉ thấy dữ liệu của mình."""
    return user.tenant_id == DEFAULT_TENANT_ID


def _visible(user: CurrentUser) -> Any:
    """Điều kiện lọc document hiển thị trên web. Document sandbox (key ds_test_) không bao giờ hiện."""
    cond = Document.sandbox == False  # noqa: E712
    return cond if _is_staff(user) else and_(cond, Document.tenant_id == user.tenant_id)


def _tenant_cond(user: CurrentUser, col: Any) -> Any:
    return true() if _is_staff(user) else col == user.tenant_id


async def _doc(session: AsyncSession, user: CurrentUser, doc_id: uuid.UUID) -> Document:
    doc = await session.get(Document, doc_id)
    if doc is None or doc.sandbox or not (_is_staff(user) or doc.tenant_id == user.tenant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy document")
    return doc


def _audit(session: AsyncSession, user: CurrentUser, action: str, target: str | None, **details: Any) -> None:
    session.add(
        AuditLog(
            tenant_id=user.tenant_id,
            actor=user.email,
            action=action,
            target_type=target.split(":")[0] if target else None,
            target_id=target.split(":", 1)[1] if target else None,
            details=details,
        )
    )


# ---------------- Đăng nhập (dev) ----------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, settings: SettingsDep) -> HTMLResponse:
    if settings.auth_mode != "dev":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return render(request, "login.html", None)


@router.post("/login")
async def login(settings: SettingsDep, email: Annotated[str, Form()]) -> RedirectResponse:
    if settings.auth_mode != "dev" or settings.app_env == "production":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    resp = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    resp.set_cookie(DEV_COOKIE, email.strip().lower(), httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout(settings: SettingsDep) -> RedirectResponse:
    resp = RedirectResponse("/login" if settings.auth_mode == "dev" else "/", status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(DEV_COOKIE)
    return resp


# ---------------- Tổng quan ----------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, user: UserDep, session: SessionDep, settings: SettingsDep
) -> HTMLResponse:
    rows = await session.execute(
        select(Document.status, func.count()).where(_visible(user)).group_by(Document.status)
    )
    counts = {s: n for s, n in rows.all()}
    processed = sum(counts.get(s, 0) for s in ("needs_review", "auto_approved", "approved", "rejected"))
    now = datetime.now(UTC)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)

    async def spent(since: datetime) -> float:
        v = await session.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_vnd), 0.0)).where(
                _tenant_cond(user, LlmCall.tenant_id), LlmCall.live.is_(True), LlmCall.created_at >= since
            )
        )
        return float(v or 0)

    live_today = await session.scalar(
        select(func.count()).where(
            _tenant_cond(user, LlmCall.tenant_id), LlmCall.live.is_(True), LlmCall.created_at >= day0
        )
    )
    top_fields = (
        await session.execute(
            select(FieldReview.field_path, func.count().label("n"))
            .join(Document, Document.id == FieldReview.document_id)
            .where(_visible(user))
            .group_by(FieldReview.field_path)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    tenant = await session.get(Tenant, user.tenant_id)
    return render(
        request,
        "dashboard.html",
        user,
        counts=counts,
        total=sum(counts.values()),
        auto_rate=(counts.get("auto_approved", 0) / processed) if processed else None,
        cost_today=await spent(day0),
        cost_month=await spent(month0),
        ledger_today=Ledger(settings.ledger_dir).spent(),
        live_today=live_today or 0,
        daily_budget=settings.llm_daily_budget_vnd,
        monthly_budget=tenant.monthly_budget_vnd if tenant else None,
        top_fields=[(FIELD_LABELS.get(p, p), n) for p, n in top_fields],
        allow_live=settings.allow_live_llm,
    )


# ---------------- Upload 2 bước ----------------


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, user: ReviewerDep, settings: SettingsDep) -> HTMLResponse:
    return render(
        request, "upload.html", user, max_file_mb=settings.max_file_mb, max_upload_mb=settings.max_upload_mb
    )


@router.post("/upload")
async def upload(
    request: Request,
    user: ReviewerDep,
    session: SessionDep,
    settings: SettingsDep,
    files: Annotated[list[UploadFile], File()],
    folder_name: Annotated[str | None, Form()] = None,
) -> Response:
    incoming: list[IncomingPdf] = []
    total, errors = 0, []
    for f in files:
        data = await f.read(settings.max_upload_mb * 1024 * 1024 + 1)
        total += len(data)
        name = f.filename or "file"
        if total > settings.max_upload_mb * 1024 * 1024:
            errors.append(f"Tổng dung lượng vượt {settings.max_upload_mb}MB")
            break
        if name.lower().endswith(".zip"):
            try:
                incoming += read_zip(data, max_file_bytes=settings.max_file_mb * 1024 * 1024)
            except UploadError as e:
                errors.append(f"{name}: {e}")
        elif is_pdf(data):
            if len(data) > settings.max_file_mb * 1024 * 1024:
                errors.append(f"{name}: vượt {settings.max_file_mb}MB")
            else:
                incoming.append(IncomingPdf((folder_name or "").strip() or None, Path(name).name, data))
        elif data:
            errors.append(f"{name}: không phải PDF/ZIP")
    if errors or not incoming:
        msg = errors or ["Chưa chọn file PDF/ZIP nào"]
        return render(
            request,
            "upload.html",
            user,
            422,
            errors=msg,
            max_file_mb=settings.max_file_mb,
            max_upload_mb=settings.max_upload_mb,
        )
    batch, _ = await create_documents(
        session,
        settings,
        user.tenant_id,  # type: ignore[arg-type]
        incoming,
        source="web",
        batch_name=", ".join(f.filename or "" for f in files)[:255],
        created_by=user.email,
    )
    return _back(f"/batches/{batch.id}/confirm")


async def _batch(session: AsyncSession, user: CurrentUser, batch_id: uuid.UUID) -> Batch:
    batch = await session.get(Batch, batch_id)
    if batch is None or batch.sandbox or not (_is_staff(user) or batch.tenant_id == user.tenant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy lô")
    return batch


@router.get("/batches/{batch_id}/confirm", response_class=HTMLResponse)
async def confirm_page(
    request: Request, batch_id: uuid.UUID, user: ReviewerDep, session: SessionDep, settings: SettingsDep
) -> HTMLResponse:
    batch = await _batch(session, user, batch_id)
    docs = list(
        await session.scalars(
            select(Document)
            .where(Document.batch_id == batch.id)
            .order_by(Document.folder_name, Document.file_name)
        )
    )
    pending = [d for d in docs if d.status == DocStatus.uploaded]
    est = estimate(settings, [(d.pages, d.pdf_type) for d in pending])
    return render(
        request, "confirm.html", user, batch=batch, docs=docs, pending=pending, est=est, settings=settings
    )


@router.post("/batches/{batch_id}/start")
async def confirm_start(
    request: Request, batch_id: uuid.UUID, user: ReviewerDep, session: SessionDep
) -> Response:
    batch = await _batch(session, user, batch_id)
    docs = await start_documents(request, session, batch.id)
    _audit(session, user, "batch.start", f"batch:{batch.id}", documents=len(docs))
    await session.commit()
    return _back(f"/documents?batch_id={batch.id}", flash=f"Đã đưa {len(docs)} document vào hàng đợi")


# ---------------- Danh sách ----------------


def _filtered(user: CurrentUser, q: dict[str, str]) -> Any:
    stmt = select(Document).where(_visible(user))
    if q.get("status"):
        stmt = stmt.where(Document.status == q["status"])
    if q.get("folder"):
        stmt = stmt.where(Document.folder_name.ilike(f"%{q['folder']}%"))
    if q.get("loai"):
        stmt = stmt.where(Document.loai_van_ban == q["loai"])
    if q.get("reviewer"):
        stmt = stmt.where(Document.reviewed_by.ilike(f"%{q['reviewer']}%"))
    if q.get("batch_id"):
        stmt = stmt.where(Document.batch_id == uuid.UUID(q["batch_id"]))
    if q.get("q"):
        like = f"%{q['q']}%"
        stmt = stmt.where(or_(Document.file_name.ilike(like), Document.so_gp.ilike(like)))
    for key, op in (("from", "ge"), ("to", "le")):
        if q.get(key):
            d = datetime.strptime(q[key], "%Y-%m-%d").date()
            stmt = stmt.where(Document.ngay_cap >= d if op == "ge" else Document.ngay_cap <= d)
    return stmt


@router.get("/documents", response_class=HTMLResponse)
async def documents(request: Request, user: UserDep, session: SessionDep) -> HTMLResponse:
    q = {k: v for k, v in request.query_params.items() if v}
    stmt = _filtered(user, q).order_by(Document.created_at.desc(), Document.folder_name, Document.file_name)
    docs = list(await session.scalars(stmt.limit(500)))
    tpl = (
        "_doc_rows.html"
        if request.headers.get("hx-request") and request.headers.get("hx-target") == "doc-table"
        else "documents.html"
    )
    return render(
        request,
        tpl,
        user,
        docs=docs,
        q=q,
        statuses=list(STATUS_LABEL.items()),
        loais=[(x.value, LOAI_LABEL[x]) for x in LoaiVanBan],
        flash=request.query_params.get("flash"),
        export_qs=urlencode(q),
    )


@router.post("/documents/bulk")
async def bulk(
    request: Request,
    user: UserDep,
    session: SessionDep,
    action: Annotated[str, Form()],
    ids: Annotated[list[uuid.UUID], Form()] = [],  # noqa: B006
) -> Response:
    if not ids:
        return _back("/documents", flash="Chưa chọn document nào")
    if action == "export":
        return _back("/export.zip?" + urlencode([("ids", str(i)) for i in ids]))
    if action != "approve" or not user.can(Role.reviewer):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Không có quyền")
    docs = await session.scalars(select(Document).where(_visible(user), Document.id.in_(ids)))
    n = 0
    for d in list(docs):
        if d.status in (DocStatus.needs_review, DocStatus.auto_approved):
            d.status, d.reviewed_by = DocStatus.approved, user.email
            await webhooks.sync_document(session, d)
            await duplicates.propagate(session, d)
            n += 1
    _audit(session, user, "document.bulk_approve", None, ids=[str(i) for i in ids], approved=n)
    await session.commit()
    return _back("/documents", flash=f"Đã duyệt {n} document")


# ---------------- Rà soát ----------------


def _field_conf(path: str, f: dict[str, Any]) -> str | None:
    if f.get("value") is None:
        return "low" if path in CRITICAL_FIELDS else None  # trường trọng yếu bị trống -> tô cam
    return f.get("confidence")


def _review_ctx(doc: Document, ext: Extraction | None) -> dict[str, Any]:
    data = ext.data if ext else {}
    groups = []
    for title, paths in FIELD_GROUPS:
        fields = []
        for p in paths:
            f = get_path(data, p) or {}
            fields.append(
                {
                    "path": p,
                    "label": FIELD_LABELS[p],
                    "value": display_value(p, f.get("value")),
                    "conf": _field_conf(p, f),
                    "note": f.get("note"),
                    "verified_by": f.get("verified_by"),
                    "page": f.get("source_page"),
                    "long": p in LONG_FIELDS,
                }
            )
        groups.append({"title": title, "fields": fields})
    return {
        "doc": doc,
        "ext": ext,
        "data": data,
        "groups": groups,
        "an_pham": data.get("an_pham") or [],
        "lanh_dao": data.get("lanh_dao") or [],
        "thay_the": data.get("gp_duoc_thay_the") or [],
        "AN_PHAM_KEYS": AN_PHAM_KEYS,
        "AN_PHAM_LABELS": AN_PHAM_LABELS,
        "loai_hinh": [(x.value, LOAI_HINH_LABEL[x]) for x in LoaiHinh],
        "chuc_vu": [(x.value, CHUC_VU_LABEL[x]) for x in ChucVu],
        "loais": [(x.value, LOAI_LABEL[x]) for x in LoaiVanBan],
        "reasons": data.get("review_reasons") or [],
    }


async def _review_page(
    request: Request,
    user: CurrentUser,
    session: AsyncSession,
    settings: Settings,
    doc: Document,
    **extra: Any,
) -> HTMLResponse:
    ext = await latest_extraction(session, doc.id)
    labels = await page_labels(settings, str(doc.id), Path(doc.storage_path))
    related = []
    if doc.folder_name:
        related = list(
            await session.scalars(
                select(Document)
                .where(
                    Document.tenant_id == doc.tenant_id,
                    Document.sandbox == False,  # noqa: E712
                    Document.folder_name == doc.folder_name,
                    Document.id != doc.id,
                )
                .order_by(Document.ngay_cap)
            )
        )
    code = extra.pop("code", 200)
    return render(
        request,
        "review.html",
        user,
        code,
        **_review_ctx(doc, ext),
        page_labels=labels,
        related=related,
        flash=request.query_params.get("flash"),
        **extra,
    )


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def review(
    request: Request, doc_id: uuid.UUID, user: UserDep, session: SessionDep, settings: SettingsDep
) -> HTMLResponse:
    return await _review_page(request, user, session, settings, await _doc(session, user, doc_id))


@router.get("/documents/{doc_id}/pages/{n}.png")
async def page_png(
    doc_id: uuid.UUID, n: int, user: UserDep, session: SessionDep, settings: SettingsDep
) -> FileResponse:
    doc = await _doc(session, user, doc_id)
    await page_labels(settings, str(doc.id), Path(doc.storage_path))
    path = page_file(settings, str(doc.id), n)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không có trang này")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/documents/{doc_id}/save")
async def save(
    request: Request, doc_id: uuid.UUID, user: ReviewerDep, session: SessionDep, settings: SettingsDep
) -> Response:
    doc = await _doc(session, user, doc_id)
    ext = await latest_extraction(session, doc.id)
    form = {k: str(v) for k, v in (await request.form()).items()}
    action = form.pop("action", "save")
    if action not in ("save", "approve", "reject"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "action không hợp lệ")
    if ext is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document chưa có kết quả trích xuất")
    res = await apply_review(session, doc, ext, form, user.email, action)
    if res.errors:
        return await _review_page(request, user, session, settings, doc, errors=res.errors, code=422)
    # Đối tác API: webhook khi đã duyệt/từ chối/sửa kết quả; bản trùng (cùng file) nhận kết quả mới
    await webhooks.sync_document(session, doc)
    await duplicates.propagate(session, doc)
    await session.commit()
    msg = {"save": "Đã lưu", "approve": "Đã duyệt", "reject": "Đã từ chối"}[action]
    if res.changed:
        version = res.extraction.version if res.extraction else ""
        msg += f" ({len(res.changed)} trường thay đổi, phiên bản {version})"
    return _back(f"/documents/{doc.id}", flash=msg)


@router.get("/documents/{doc_id}/history", response_class=HTMLResponse)
async def history(request: Request, doc_id: uuid.UUID, user: UserDep, session: SessionDep) -> HTMLResponse:
    doc = await _doc(session, user, doc_id)
    reviews = list(
        await session.scalars(
            select(FieldReview)
            .where(FieldReview.document_id == doc.id)
            .order_by(FieldReview.created_at.desc())
        )
    )
    versions = list(
        await session.scalars(
            select(Extraction).where(Extraction.document_id == doc.id).order_by(Extraction.version.desc())
        )
    )
    return render(request, "_history.html", user, reviews=reviews, versions=versions)


@router.get("/review/next")
async def next_doc(user: UserDep, session: SessionDep, after: uuid.UUID | None = None) -> Response:
    stmt = select(Document.id).where(_visible(user), Document.status == DocStatus.needs_review)
    if after:
        stmt = stmt.where(Document.id != after)
    doc_id = await session.scalar(stmt.order_by(Document.created_at, Document.file_name).limit(1))
    if doc_id is None:
        return _back("/documents", flash="Không còn document cần rà soát")
    return _back(f"/documents/{doc_id}")


@router.get("/documents/{doc_id}/rerun", response_class=HTMLResponse)
async def rerun_confirm(
    request: Request, doc_id: uuid.UUID, user: ReviewerDep, session: SessionDep, settings: SettingsDep
) -> HTMLResponse:
    doc = await _doc(session, user, doc_id)
    est = estimate(settings, [(doc.pages, doc.pdf_type)])
    return render(request, "_rerun.html", user, doc=doc, est=est, settings=settings)


@router.post("/documents/{doc_id}/rerun")
async def rerun(
    request: Request,
    doc_id: uuid.UUID,
    user: ReviewerDep,
    session: SessionDep,
    confirm: Annotated[str, Form()] = "",
    bypass_cache: Annotated[str, Form()] = "",
) -> Response:
    if confirm != "1":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cần xác nhận chạy lại (tốn phí)")
    doc = await _doc(session, user, doc_id)
    doc.status, doc.error = DocStatus.uploaded, None
    _audit(session, user, "document.rerun", f"document:{doc.id}", bypass_cache=bypass_cache == "1")
    await session.commit()
    await request.app.state.enqueue(doc.id, read_cache=bypass_cache != "1", rerun=True)
    return _back(f"/documents/{doc.id}", flash="Đã đưa vào hàng đợi chạy lại")


# ---------------- Xuất dữ liệu ----------------


@router.get("/documents/{doc_id}/export.xlsx")
async def export_one(doc_id: uuid.UUID, user: UserDep, session: SessionDep) -> Response:
    doc = await _doc(session, user, doc_id)
    ext = await latest_extraction(session, doc.id)
    if ext is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Chưa có kết quả")
    body = to_xlsx(GiayPhep.model_validate(ext.data), folder=doc.folder_name)
    return Response(
        body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(_stem(doc.file_name))}.xlsx"},
    )


@router.get("/export.zip")
async def export_zip(
    request: Request,
    user: UserDep,
    session: SessionDep,
    ids: Annotated[list[uuid.UUID], Query()] = [],  # noqa: B006
    only_approved: str = "",
) -> Response:
    if ids:
        stmt = select(Document).where(_visible(user), Document.id.in_(ids))
    else:
        q = {k: v for k, v in request.query_params.items() if v and k not in ("only_approved", "ids")}
        stmt = _filtered(user, q)
    if only_approved == "1":
        stmt = stmt.where(Document.status.in_([DocStatus.approved, DocStatus.auto_approved]))
    items = []
    for d in await session.scalars(stmt.order_by(Document.folder_name, Document.file_name)):
        ext = await latest_extraction(session, d.id)
        if ext is not None:
            items.append((d, ext))
    if not items:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không có document nào có kết quả để xuất")
    _audit(session, user, "export.zip", None, documents=len(items), only_approved=only_approved == "1")
    await session.commit()
    name = f"DocSense_{datetime.now(UTC):%Y%m%d_%H%M}.zip"
    return Response(
        build_zip(items),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---------------- Quản trị ----------------


@router.get("/admin", response_class=HTMLResponse)
async def admin(request: Request, user: AdminDep, session: SessionDep, settings: SettingsDep) -> HTMLResponse:
    users = list(await session.scalars(select(User).order_by(User.email)))
    tenants = list(await session.scalars(select(Tenant).order_by(Tenant.created_at)))
    keys = list(await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))
    logs = list(await session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)))
    return render(
        request,
        "admin.html",
        user,
        users=users,
        tenants=tenants,
        tenant_names={t.id: t.name for t in tenants},
        keys=keys,
        logs=logs,
        roles=[r.value for r in Role],
        settings=settings,
        new_key=request.query_params.get("new_key"),
        flash=request.query_params.get("flash"),
        now=datetime.now(UTC),
    )


@router.post("/admin/users")
async def admin_user(
    user: AdminDep,
    session: SessionDep,
    email: Annotated[str, Form()],
    role: Annotated[Role, Form()],
    active: Annotated[str, Form()] = "1",
) -> Response:
    email = email.strip().lower()
    u = await session.scalar(select(User).where(User.email == email))
    if u is None:
        u = User(email=email, tenant_id=user.tenant_id)
        session.add(u)
    if u.email == user.email and (role != Role.admin or active != "1"):
        return _back("/admin", flash="Không thể tự hạ quyền/khóa chính mình")
    u.role, u.active = role, active == "1"
    _audit(session, user, "user.upsert", f"user:{email}", role=role.value, active=u.active)
    await session.commit()
    return _back("/admin", flash=f"Đã cập nhật {email}")


@router.post("/admin/tenants/{tenant_id}")
async def admin_tenant(
    tenant_id: uuid.UUID,
    user: AdminDep,
    session: SessionDep,
    name: Annotated[str, Form()],
    require_human_review: Annotated[str, Form()] = "",
    monthly_budget_vnd: Annotated[str, Form()] = "",
    monthly_page_quota: Annotated[str, Form()] = "",
    rate_limit_per_minute: Annotated[str, Form()] = "",
) -> Response:
    t = await session.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    t.name = name.strip() or t.name
    t.require_human_review = require_human_review == "1"
    t.monthly_budget_vnd = float(monthly_budget_vnd) if monthly_budget_vnd.strip() else None  # đồng
    t.monthly_page_quota = int(monthly_page_quota) if monthly_page_quota.strip() else None
    t.rate_limit_per_minute = int(rate_limit_per_minute) if rate_limit_per_minute.strip() else None
    _audit(
        session,
        user,
        "tenant.update",
        f"tenant:{t.id}",
        require_human_review=t.require_human_review,
        monthly_budget_vnd=t.monthly_budget_vnd,
        monthly_page_quota=t.monthly_page_quota,
        rate_limit_per_minute=t.rate_limit_per_minute,
    )
    await session.commit()
    return _back("/admin", flash=f"Đã cập nhật tenant {t.name}")


@router.post("/admin/tenants/{tenant_id}/webhook-secret")
async def admin_tenant_secret(tenant_id: uuid.UUID, user: AdminDep, session: SessionDep) -> Response:
    """Tạo/xoay secret ký webhook. Secret cũ mất hiệu lực ngay -> báo đối tác cập nhật."""
    t = await session.get(Tenant, tenant_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    t.webhook_secret = webhooks.new_secret()
    _audit(session, user, "tenant.webhook_secret_rotate", f"tenant:{t.id}")
    await session.commit()
    return _back("/admin", flash=f"Đã tạo webhook secret mới cho {t.name}")


@router.post("/admin/tenants")
async def admin_tenant_new(
    user: AdminDep, session: SessionDep, slug: Annotated[str, Form()], name: Annotated[str, Form()]
) -> Response:
    t = Tenant(
        slug=slug.strip().lower(),
        name=name.strip(),
        require_human_review=True,
        webhook_secret=webhooks.new_secret(),
    )
    session.add(t)
    _audit(session, user, "tenant.create", f"tenant:{slug}")
    await session.commit()
    return _back("/admin", flash=f"Đã tạo tenant {t.name}")


@router.post("/admin/keys")
async def admin_key_new(
    user: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    tenant_id: Annotated[uuid.UUID, Form()],
    name: Annotated[str, Form()],
    sandbox: Annotated[str, Form()] = "",
    expires_days: Annotated[str, Form()] = "",
) -> Response:
    days = int(expires_days) if expires_days.strip() else None
    k, key = new_api_key(settings, tenant_id, name.strip(), sandbox=sandbox == "1", expires_days=days)
    session.add(k)
    _audit(session, user, "api_key.create", f"api_key:{k.prefix}", tenant=str(tenant_id), sandbox=k.sandbox)
    await session.commit()
    # Khóa thô chỉ hiện 1 lần (không lưu)
    return _back("/admin", new_key=key)


@router.post("/admin/keys/{key_id}/revoke")
async def admin_key_revoke(key_id: uuid.UUID, user: AdminDep, session: SessionDep) -> Response:
    k = await session.get(ApiKey, key_id)
    if k is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    k.revoked_at = datetime.now(UTC)
    _audit(session, user, "api_key.revoke", f"api_key:{k.prefix}")
    await session.commit()
    return _back("/admin", flash=f"Đã thu hồi {k.prefix}…")
