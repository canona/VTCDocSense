import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app import __version__
from app.api.partner import errors as partner_errors
from app.api.partner import routes as partner_routes
from app.api.routes import health, internal
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine
from app.providers import build_provider
from app.services.ratelimit import RedisRateLimiter
from app.web import routes as web_routes
from app.web.auth import LoginRequired

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.redis = Redis.from_url(settings.redis_url)
    app.state.rate_limiter = RedisRateLimiter(app.state.redis)
    app.state.provider = build_provider(settings)
    pool: list[ArqRedis] = []  # tạo lazy: API vẫn khởi động được khi Redis chưa sẵn sàng

    async def enqueue(doc_id: uuid.UUID, read_cache: bool = True, *, rerun: bool = False) -> None:
        if not pool:
            pool.append(await create_pool(RedisSettings.from_dsn(settings.redis_url)))
        # Lần đầu: _job_id cố định theo document -> bấm 2 lần không xếp hàng trùng.
        # Chạy lại: id mới (arq giữ kết quả job cũ ~1 giờ và bỏ qua job trùng id).
        job_id = f"doc:{doc_id}:{uuid.uuid4().hex[:8]}" if rerun else f"doc:{doc_id}"
        job = await pool[0].enqueue_job("process_document", str(doc_id), read_cache, _job_id=job_id)
        if job is None:
            log.warning("job đã có trong hàng đợi/kết quả, bỏ qua", extra={"job_id": job_id})

    app.state.enqueue = enqueue
    try:
        yield
    finally:
        for p in pool:
            await p.aclose()
        await app.state.provider.aclose()
        await app.state.redis.aclose()
        await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = settings.enable_docs
    app = FastAPI(
        title="VTCDocSense API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.include_router(health.router)
    app.include_router(internal.router)
    app.include_router(partner_routes.router)
    app.include_router(web_routes.router)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    partner_errors.install(app)
    app.middleware("http")(_partner_middleware)
    if settings.partner_docs:
        _partner_docs(app)

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    return app


PARTNER_DESCRIPTION = """API cho đối tác: gửi PDF Giấy phép báo chí, nhận kết quả JSON/Excel.

Xác thực: `Authorization: Bearer <api_key>`. Key `ds_test_…` = sandbox (miễn phí, kết quả mẫu, không gọi AI).
Hướng dẫn đầy đủ: docs/PARTNER_API.md."""
_PUBLIC_V1 = ("/v1/docs", "/v1/openapi.json")
access_log = logging.getLogger("app.api.access")


async def _partner_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Chỉ cho /v1: request id, chặn sớm khi thiếu key (trước khi đọc body upload), header rate limit,
    log truy cập 1 dòng (chỉ metadata - không bao giờ ghi nội dung file/body)."""
    path = request.url.path
    if not path.startswith(partner_errors.PREFIX) or path in _PUBLIC_V1:
        return await call_next(request)
    start = time.perf_counter()
    rid = partner_errors.request_id(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        resp: Response = partner_errors.error_response(
            request, "missing_api_key", partner_errors.ERROR_CODES["missing_api_key"][1], 401,
            {"WWW-Authenticate": "Bearer"},
        )  # fmt: skip
    else:
        resp = await call_next(request)
    resp.headers["X-Request-ID"] = rid
    rate = getattr(request.state, "rate", None)
    if rate is not None:
        resp.headers["X-RateLimit-Limit"] = str(rate.limit)
        resp.headers["X-RateLimit-Remaining"] = str(rate.remaining)
        resp.headers["X-RateLimit-Reset"] = str(rate.reset_s)
    access_log.info(
        "api",
        extra={
            "request_id": rid,
            "method": request.method,
            "path": path,
            "status": resp.status_code,
            "key": getattr(request.state, "api_key_prefix", None),
            "ms": int((time.perf_counter() - start) * 1000),
        },
    )
    return resp


def _partner_docs(app: FastAPI) -> None:
    cache: dict[str, Any] = {}

    @app.get("/v1/openapi.json", include_in_schema=False)
    async def partner_openapi() -> dict[str, Any]:
        if not cache:
            cache.update(
                get_openapi(
                    title="VTCDocSense Partner API",
                    version=__version__,
                    description=PARTNER_DESCRIPTION,
                    routes=partner_routes.router.routes,
                    servers=[{"url": "/"}],
                )
            )
        return cache

    @app.get("/v1/docs", include_in_schema=False)
    async def partner_swagger() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/v1/openapi.json", title="VTCDocSense Partner API")


app = create_app()
