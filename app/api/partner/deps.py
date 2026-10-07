"""Xác thực API key (Bearer), kiểm tra scope, giới hạn request/phút."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.partner.errors import ApiError
from app.api.routes.internal import get_session
from app.core.config import Settings, get_settings
from app.core.logging import tenant_var
from app.db.models import ApiKey, Tenant
from app.services.api_keys import Scope, authenticate
from app.services.partner import rate_limit
from app.services.ratelimit import MemoryRateLimiter, RateLimiter

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ApiKey",
    description="`Authorization: Bearer ds_live_…` (thật) hoặc `Bearer ds_test_…` (sandbox, miễn phí)",
)
_fallback_limiter = MemoryRateLimiter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass
class Partner:
    key: ApiKey
    tenant: Tenant

    @property
    def sandbox(self) -> bool:
        return self.key.sandbox


def require(scope: Scope | None) -> Callable[..., Awaitable[Partner]]:
    async def dep(
        request: Request,
        session: SessionDep,
        settings: SettingsDep,
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Partner:
        res = await authenticate(session, settings, creds.credentials if creds else None)
        if res.key is None:
            assert res.error is not None
            raise ApiError(res.error, headers={"WWW-Authenticate": "Bearer"})
        key = res.key
        if scope is not None and scope.value not in (key.scopes or []):
            raise ApiError("insufficient_scope", f"API key thiếu scope '{scope.value}'")
        tenant = await session.get(Tenant, key.tenant_id)
        if tenant is None:
            raise ApiError("invalid_api_key", headers={"WWW-Authenticate": "Bearer"})
        request.state.api_key_prefix = key.prefix
        tenant_var.set(tenant.slug)
        limiter: RateLimiter = getattr(request.app.state, "rate_limiter", None) or _fallback_limiter
        rr = await limiter.hit(str(key.id), rate_limit(tenant, settings))
        request.state.rate = rr
        if not rr.allowed:
            raise ApiError(
                "rate_limited",
                f"Vượt {rr.limit} request/phút; thử lại sau {rr.reset_s} giây",
                headers={"Retry-After": str(rr.reset_s)},
            )
        return Partner(key, tenant)

    return dep


ReadDep = Annotated[Partner, Depends(require(Scope.documents_read))]
WriteDep = Annotated[Partner, Depends(require(Scope.documents_write))]
AnyKeyDep = Annotated[Partner, Depends(require(None))]
