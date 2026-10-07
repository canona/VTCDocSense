"""API key đối tác: sinh, hash (chỉ lưu hash), xác thực.

Dạng key: `ds_live_<32 ký tự>` (thật) | `ds_test_<32 ký tự>` (sandbox, không bao giờ gọi LLM).
`prefix` = 14 ký tự đầu, lưu rõ để nhận diện trong giao diện/log; phần còn lại chỉ tồn tại dưới dạng hash.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import ApiKey

LIVE_PREFIX, TEST_PREFIX = "ds_live_", "ds_test_"
PREFIX_LEN = 14


class Scope(StrEnum):
    documents_write = "documents:write"  # upload, gửi lại webhook
    documents_read = "documents:read"  # trạng thái, kết quả, export, usage


DEFAULT_SCOPES = [s.value for s in Scope]


def hash_api_key(key: str, settings: Settings) -> str:
    pepper = settings.api_key_pepper.get_secret_value() if settings.api_key_pepper else ""
    return hashlib.sha256(f"{pepper}:{key}".encode()).hexdigest()


def generate_key(sandbox: bool) -> str:
    return (TEST_PREFIX if sandbox else LIVE_PREFIX) + secrets.token_urlsafe(24)


def new_api_key(
    settings: Settings,
    tenant_id: uuid.UUID,
    name: str,
    *,
    sandbox: bool,
    scopes: list[str] | None = None,
    expires_days: int | None = None,
) -> tuple[ApiKey, str]:
    """Trả (bản ghi, key thô). Key thô chỉ hiện 1 lần cho người tạo."""
    key = generate_key(sandbox)
    row = ApiKey(
        tenant_id=tenant_id,
        name=name,
        prefix=key[:PREFIX_LEN],
        key_hash=hash_api_key(key, settings),
        scopes=list(scopes or DEFAULT_SCOPES),
        sandbox=sandbox,
        expires_at=datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None,
    )
    return row, key


@dataclass
class AuthResult:
    key: ApiKey | None
    # missing_api_key | invalid_api_key | api_key_revoked | api_key_expired.
    # Key sai định dạng và key không tồn tại cùng mã invalid_api_key (không tiết lộ key có tồn tại)
    error: str | None = None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite trả datetime không có tz


async def authenticate(session: AsyncSession, settings: Settings, raw: str | None) -> AuthResult:
    if not raw:
        return AuthResult(None, "missing_api_key")
    if not raw.startswith((LIVE_PREFIX, TEST_PREFIX)) or len(raw) < PREFIX_LEN + 16:
        return AuthResult(None, "invalid_api_key")
    digest = hash_api_key(raw, settings)
    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == digest))
    if key is None or not hmac.compare_digest(key.key_hash, digest):
        return AuthResult(None, "invalid_api_key")
    # Loại key phải khớp prefix: ds_test_ luôn là sandbox, ds_live_ không bao giờ là sandbox
    if key.sandbox != raw.startswith(TEST_PREFIX):
        return AuthResult(None, "invalid_api_key")
    now = datetime.now(UTC)
    if key.revoked_at is not None:
        return AuthResult(None, "api_key_revoked")
    if key.expires_at is not None and _aware(key.expires_at) <= now:
        return AuthResult(None, "api_key_expired")
    if key.last_used_at is None or now - _aware(key.last_used_at) > timedelta(minutes=1):
        key.last_used_at = now
        await session.commit()
    return AuthResult(key)
