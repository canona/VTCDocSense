"""Đăng nhập giao diện web.

- AUTH_MODE=cf_access: Cloudflare Access chèn header `Cf-Access-Jwt-Assertion` (JWT RS256). Xác minh chữ ký
  bằng khóa công khai `https://<team>/cdn-cgi/access/certs`, kiểm tra `aud` (CF_ACCESS_AUD) và `iss`.
- AUTH_MODE=dev: email lấy từ cookie do trang /login đặt. Bị chặn khi APP_ENV=production.

Người dùng mới đăng nhập lần đầu -> tạo bản ghi `users` vai trò viewer
(email trong BOOTSTRAP_ADMIN_EMAILS -> admin).
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.internal import get_session
from app.core.config import Settings, get_settings
from app.db.models import DEFAULT_TENANT_ID, Role, User

log = logging.getLogger(__name__)

DEV_COOKIE = "ds_dev_email"
CF_HEADER = "cf-access-jwt-assertion"
ROLE_RANK = {Role.viewer: 0, Role.reviewer: 1, Role.admin: 2}


class LoginRequired(Exception):
    """Chưa đăng nhập (dev) -> chuyển tới /login."""


@dataclass
class CurrentUser:
    id: str
    email: str
    role: Role
    tenant_id: object

    def can(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


@lru_cache(maxsize=4)
def _jwks_client(team_domain: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)


def verify_cf_jwt(token: str, settings: Settings, jwks: jwt.PyJWKClient | None = None) -> str:
    """Trả email trong JWT Cloudflare Access; ném HTTPException 401 nếu không hợp lệ."""
    team, aud = settings.cf_access_team_domain, settings.cf_access_aud
    if not team or not aud:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Thiếu CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD"
        )
    try:
        key = (jwks or _jwks_client(team)).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team}")
    except jwt.PyJWTError as e:
        log.warning("JWT Cloudflare Access không hợp lệ", extra={"error": str(e)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Phiên Cloudflare Access không hợp lệ") from e
    email = claims.get("email")
    if not email:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "JWT không có email")
    return str(email).lower()


def request_email(request: Request, settings: Settings) -> str:
    if settings.auth_mode == "dev":
        if settings.app_env == "production":
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "AUTH_MODE=dev bị cấm ở production")
        email = request.cookies.get(DEV_COOKIE)
        if not email:
            raise LoginRequired
        return email.lower()
    token = request.headers.get(CF_HEADER)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Thiếu header Cf-Access-Jwt-Assertion")
    return verify_cf_jwt(token, settings)


async def get_current_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    email = request_email(request, settings)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        role = Role.admin if email in settings.admin_emails else Role.viewer
        user = User(email=email, role=role, tenant_id=DEFAULT_TENANT_ID)
        session.add(user)
        await session.commit()
    elif email in settings.admin_emails and user.role != Role.admin:
        user.role = Role.admin
        await session.commit()
    if not user.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Tài khoản đã bị khóa")
    return CurrentUser(str(user.id), user.email, Role(user.role), user.tenant_id)


UserDep = Annotated[CurrentUser, Depends(get_current_user)]


def require(role: Role):  # type: ignore[no-untyped-def]
    async def dep(user: UserDep) -> CurrentUser:
        if not user.can(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Cần quyền {role.value}")
        return user

    return dep
