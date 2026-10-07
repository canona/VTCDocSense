"""Tự xóa dữ liệu sau RETENTION_DAYS (cron worker mỗi ngày).

- File PDF gốc + ảnh trang cache của document cũ hơn RETENTION_DAYS -> xóa, đánh dấu `purged_at`
  (kết quả trích xuất JSON vẫn giữ để đối tác tải lại).
- Cache phản hồi LLM trên đĩa (chứa nội dung trích xuất) cũ hơn RETENTION_DAYS -> xóa.
- Idempotency-Key quá IDEMPOTENCY_TTL_HOURS, webhook đã xong cũ hơn RETENTION_DAYS -> xóa bản ghi.
"""

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Document, IdempotencyKey, WebhookDelivery, WebhookStatus

log = logging.getLogger(__name__)


@dataclass
class PurgeResult:
    documents: int = 0
    cache_files: int = 0
    idempotency_keys: int = 0
    webhooks: int = 0


def _remove_files(settings: Settings, doc: Document) -> None:
    Path(doc.storage_path).unlink(missing_ok=True)
    shutil.rmtree(settings.data_dir / "page_cache" / str(doc.id), ignore_errors=True)


def _purge_cache(root: Path, cutoff_ts: float) -> int:
    n = 0
    for f in root.glob("*/*.json") if root.exists() else []:
        if f.stat().st_mtime < cutoff_ts:
            f.unlink(missing_ok=True)
            n += 1
    return n


async def purge(session: AsyncSession, settings: Settings, now: datetime | None = None) -> PurgeResult:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=settings.retention_days)
    res = PurgeResult()
    docs = list(
        await session.scalars(
            select(Document).where(Document.created_at < cutoff, Document.purged_at.is_(None)).limit(1000)
        )
    )
    for d in docs:
        await asyncio.to_thread(_remove_files, settings, d)
        d.purged_at = now
        res.documents += 1
    res.cache_files = await asyncio.to_thread(_purge_cache, settings.cache_dir, cutoff.timestamp())
    r = await session.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.created_at < now - timedelta(hours=settings.idempotency_ttl_hours)
        )
    )
    res.idempotency_keys = r.rowcount  # type: ignore[attr-defined]
    r = await session.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.created_at < cutoff,
            WebhookDelivery.status.in_([WebhookStatus.succeeded, WebhookStatus.failed]),
        )
    )
    res.webhooks = r.rowcount  # type: ignore[attr-defined]
    await session.commit()
    log.info("retention", extra={"retention_days": settings.retention_days, **res.__dict__})
    return res
