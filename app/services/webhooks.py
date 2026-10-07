"""Webhook đối tác: tạo sự kiện khi document/batch tới trạng thái cuối, ký HMAC-SHA256, gửi có retry.

- Sự kiện lưu bảng `webhook_deliveries` (bền vững); worker gửi (cron mỗi 10 giây + ngay sau khi xử lý).
- Chữ ký: header `X-DocSense-Signature: t=<unix>,v1=<hex>` với
  `hex = HMAC_SHA256(webhook_secret của tenant, f"{t}.{body}")`.
- Retry khi không nhận 2xx: sau 30s, 2m, 10m, 30m, 2h, 6h, 12h (WEBHOOK_MAX_ATTEMPTS lượt), rồi `failed`;
  gửi lại bằng `POST /v1/documents/{id}/webhook/resend`.
- Chống SSRF: chỉ https, host phải phân giải ra IP công khai (trừ khi WEBHOOK_ALLOW_PRIVATE=true).
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core.config import Settings
from app.db.models import Batch, Document, Extraction, Tenant, WebhookDelivery, WebhookStatus
from app.services.partner import (
    FINAL_PUBLIC,
    PublicStatus,
    batch_status,
    document_view,
    public_status,
    result_hash,
)
from app.services.queries import latest_extraction

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-DocSense-Signature"
BACKOFF_S = [30, 120, 600, 1800, 7200, 21600, 43200]  # chờ trước lượt thứ 2, 3, ...
SIGNATURE_TOLERANCE_S = 300


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def ensure_secret(tenant: Tenant) -> str:
    if not tenant.webhook_secret:
        tenant.webhook_secret = new_secret()
    return tenant.webhook_secret


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    t = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify_signature(
    secret: str, body: bytes, header: str, *, tolerance_s: int = SIGNATURE_TOLERANCE_S, now: int | None = None
) -> bool:
    """Phía đối tác: kiểm tra chữ ký + chống phát lại (timestamp lệch quá `tolerance_s` -> sai)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((int(time.time()) if now is None else now) - t) > tolerance_s:
        return False
    expected = sign(secret, body, t).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


# ---------------- Kiểm tra URL (SSRF) ----------------


def _ip_ok(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_global


def validate_url(url: str, settings: Settings) -> str | None:
    """Kiểm tra cú pháp lúc nhận request. Trả thông báo lỗi hoặc None."""
    if len(url) > 2048:
        return "webhook_url dài quá 2048 ký tự"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "webhook_url không hợp lệ"
    allowed = ("https", "http") if settings.webhook_allow_private else ("https",)
    if parts.scheme not in allowed or not parts.hostname:
        return "webhook_url phải là URL https://"
    if parts.username or parts.password:
        return "webhook_url không được chứa thông tin đăng nhập"
    if not settings.webhook_allow_private:
        host = parts.hostname
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return "webhook_url không được trỏ tới địa chỉ nội bộ"
        try:
            if not _ip_ok(host):
                return "webhook_url không được trỏ tới địa chỉ nội bộ"
        except ValueError:
            pass  # là tên miền: kiểm tra IP khi gửi
    return None


async def _resolves_public(host: str, port: int) -> bool:
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(_ip_ok(str(i[4][0])) for i in infos)


# ---------------- Tạo sự kiện ----------------


def _event_payload(event: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex}",
        "object": "event",
        "type": event,
        "created_at": datetime.now(UTC).isoformat(),
        "data": data,
    }


def _queue(
    session: AsyncSession, tenant: Tenant, url: str, event: str, data: dict[str, Any], **target: Any
) -> WebhookDelivery:
    ensure_secret(tenant)
    d = WebhookDelivery(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        event=event,
        url=url,
        payload=_event_payload(event, data),
        status=WebhookStatus.pending,
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        **target,
    )
    session.add(d)
    return d


async def document_event(
    session: AsyncSession, doc: Document, tenant: Tenant | None = None
) -> tuple[str, dict[str, Any], Extraction | None] | None:
    """(event, data, bản trích xuất) cho trạng thái hiện tại nếu đã là trạng thái cuối, không thì None."""
    tenant = tenant or await session.get(Tenant, doc.tenant_id)
    assert tenant is not None
    st = public_status(doc, tenant.require_human_review)
    if st not in FINAL_PUBLIC:
        return None
    ext = await latest_extraction(session, doc.id) if st == PublicStatus.completed else None
    view = document_view(doc, tenant.require_human_review, ext, include_result=False)
    return f"document.{st.value}", {"document": view}, ext


async def sync_document(session: AsyncSession, doc: Document) -> WebhookDelivery | None:
    """Gọi sau mỗi lần trạng thái/kết quả document thay đổi (chưa commit; người gọi commit).

    Chỉ tạo sự kiện khi trạng thái cuối (hoặc kết quả sau trạng thái cuối) khác lần đã báo:
    `document.completed|failed|rejected`, hoặc `document.updated` khi kết quả đã báo bị sửa (rà soát lại).
    """
    tenant = await session.get(Tenant, doc.tenant_id)
    assert tenant is not None
    ev = await document_event(session, doc, tenant)
    if ev is None:
        return None
    event, data, ext = ev
    st = data["document"]["status"]
    state = f"{st}:{result_hash(ext.data if ext else None)}"
    if state == doc.webhook_state:
        return None
    if doc.webhook_state and doc.webhook_state.startswith("completed:") and st == PublicStatus.completed:
        event = "document.updated"
    doc.webhook_state = state
    delivery = (
        _queue(session, tenant, doc.webhook_url, event, data, document_id=doc.id) if doc.webhook_url else None
    )
    if doc.batch_id:
        await _sync_batch(session, tenant, doc.batch_id)
    return delivery


async def _sync_batch(session: AsyncSession, tenant: Tenant, batch_id: uuid.UUID) -> None:
    batch = await session.get(Batch, batch_id)
    if batch is None or not batch.webhook_url or batch.webhook_state == "completed":
        return
    docs = list(await session.scalars(select(Document).where(Document.batch_id == batch.id)))
    sts = [public_status(d, tenant.require_human_review) for d in docs]
    if batch_status(sts) != "completed":
        return
    counts: dict[str, int] = {}
    for s in sts:
        counts[s.value] = counts.get(s.value, 0) + 1
    batch.webhook_state = "completed"
    data = {"batch": {"id": str(batch.id), "name": batch.name, "status": "completed", "counts": counts}}
    _queue(session, tenant, batch.webhook_url, "batch.completed", data, batch_id=batch.id)


async def resend(session: AsyncSession, doc: Document, url: str | None = None) -> WebhookDelivery | None:
    """Gửi lại trạng thái cuối hiện tại (đối tác bỏ lỡ webhook). None nếu chưa có trạng thái cuối."""
    ev = await document_event(session, doc)
    target = url or doc.webhook_url
    if ev is None or not target:
        return None
    tenant = await session.get(Tenant, doc.tenant_id)
    assert tenant is not None
    return _queue(session, tenant, target, ev[0], ev[1], document_id=doc.id)


# ---------------- Gửi ----------------


async def _claim(session: AsyncSession, d: WebhookDelivery, settings: Settings) -> bool:
    """Khóa bản ghi (UPDATE có điều kiện) để 2 tiến trình không gửi trùng."""
    now = datetime.now(UTC)
    res = await session.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == d.id,
            or_(
                and_(WebhookDelivery.status == WebhookStatus.pending, WebhookDelivery.next_attempt_at <= now),
                and_(WebhookDelivery.status == WebhookStatus.sending, WebhookDelivery.locked_until < now),
            ),
        )
        .values(
            status=WebhookStatus.sending,
            locked_until=now + timedelta(seconds=settings.webhook_timeout_s + 60),
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return bool(res.rowcount == 1)  # type: ignore[attr-defined]


async def _send(
    d: WebhookDelivery, secret: str, settings: Settings, client: httpx.AsyncClient
) -> tuple[int | None, str | None]:
    parts = urlsplit(d.url)
    if not settings.webhook_allow_private:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme != "https" or not await _resolves_public(parts.hostname or "", port):
            return None, "URL trỏ tới địa chỉ nội bộ hoặc không phân giải được"
    body = json.dumps(d.payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"VTCDocSense-Webhook/{__version__}",
        "X-DocSense-Event": d.event,
        "X-DocSense-Delivery": str(d.id),
        SIGNATURE_HEADER: sign(secret, body),
    }
    try:
        r = await client.post(d.url, content=body, headers=headers, timeout=settings.webhook_timeout_s)
    except httpx.HTTPError as e:
        return None, type(e).__name__
    return r.status_code, None if r.is_success else f"HTTP {r.status_code}"


async def deliver(
    session: AsyncSession, d: WebhookDelivery, settings: Settings, client: httpx.AsyncClient
) -> bool:
    if not await _claim(session, d, settings):
        return False
    await session.refresh(d)
    tenant = await session.get(Tenant, d.tenant_id)
    assert tenant is not None
    code, err = await _send(d, ensure_secret(tenant), settings, client)
    now = datetime.now(UTC)
    d.attempts += 1
    d.last_status_code, d.last_error, d.locked_until = code, err, None
    if err is None:
        d.status, d.delivered_at, d.next_attempt_at = WebhookStatus.succeeded, now, None
    elif d.attempts >= settings.webhook_max_attempts:
        d.status, d.next_attempt_at = WebhookStatus.failed, None
    else:
        delay = BACKOFF_S[min(d.attempts - 1, len(BACKOFF_S) - 1)]
        d.status, d.next_attempt_at = WebhookStatus.pending, now + timedelta(seconds=delay)
    await session.commit()
    log.info(
        "webhook",
        extra={
            "delivery": str(d.id),
            "event": d.event,
            "attempt": d.attempts,
            "code": code,
            "result": d.status,
        },
    )
    return err is None


async def deliver_due(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    document_id: uuid.UUID | None = None,
    limit: int = 50,
) -> int:
    """Gửi các sự kiện tới hạn. Trả số lượt gửi thành công."""
    now = datetime.now(UTC)
    stmt = select(WebhookDelivery).where(
        or_(
            and_(WebhookDelivery.status == WebhookStatus.pending, WebhookDelivery.next_attempt_at <= now),
            and_(WebhookDelivery.status == WebhookStatus.sending, WebhookDelivery.locked_until < now),
        )
    )
    if document_id is not None:
        stmt = stmt.where(WebhookDelivery.document_id == document_id)
    due = list(await session.scalars(stmt.order_by(WebhookDelivery.next_attempt_at).limit(limit)))
    ok = 0
    for d in due:
        ok += await deliver(session, d, settings, client)
    return ok
