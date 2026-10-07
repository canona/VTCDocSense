"""Header `Idempotency-Key`: request lặp lại (mạng chập chờn, client retry) nhận lại đúng response lần đầu.

- Phạm vi: theo tenant + chế độ (live/sandbox); hết hạn sau IDEMPOTENCY_TTL_HOURS.
- Cùng key nhưng nội dung khác (tham số/file) -> 422 idempotency_key_reused.
- Lần đầu còn đang chạy -> 409 idempotency_in_progress.
- Request lỗi không được lưu (sửa lỗi rồi gửi lại với cùng key được).
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.partner.deps import Partner
from app.api.partner.errors import ApiError
from app.db.models import IdempotencyKey

REPLAY_HEADER = "Idempotent-Replayed"


def fingerprint(endpoint: str, params: dict[str, Any], file_hashes: list[tuple[str, str]]) -> str:
    raw = json.dumps({"ep": endpoint, "params": params, "files": file_hashes}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Replay:
    status_code: int
    body: dict[str, Any]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def begin(
    session: AsyncSession, partner: Partner, key: str, fp: str, ttl_hours: int
) -> IdempotencyKey | Replay:
    if not key or len(key) > 255:
        raise ApiError("invalid_request", "Idempotency-Key phải dài 1..255 ký tự")
    q = select(IdempotencyKey).where(
        IdempotencyKey.tenant_id == partner.tenant.id,
        IdempotencyKey.sandbox == partner.sandbox,
        IdempotencyKey.key == key,
    )
    row = await session.scalar(q)
    if row is not None and _aware(row.created_at) < datetime.now(UTC) - timedelta(hours=ttl_hours):
        await session.delete(row)
        await session.commit()
        row = None
    if row is not None:
        if row.fingerprint != fp:
            raise ApiError("idempotency_key_reused")
        if row.status_code is None or row.response is None:
            raise ApiError("idempotency_in_progress")
        return Replay(row.status_code, row.response)
    row = IdempotencyKey(tenant_id=partner.tenant.id, sandbox=partner.sandbox, key=key, fingerprint=fp)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:  # request song song cùng key
        await session.rollback()
        raise ApiError("idempotency_in_progress") from None
    return row


async def finish(session: AsyncSession, row: IdempotencyKey, status_code: int, body: dict[str, Any]) -> None:
    row.status_code, row.response = status_code, body
    await session.commit()


async def abort(session: AsyncSession, row: IdempotencyKey) -> None:
    await session.rollback()
    await session.delete(row)
    await session.commit()
