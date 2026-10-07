"""Cấu hình arq worker. Chạy: `arq app.worker.settings.WorkerSettings`."""

import uuid
from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_sessionmaker
from app.providers import build_providers
from app.services.documents import process_document as _process_document


async def ping(ctx: dict[str, Any]) -> str:
    """Task kiểm tra worker hoạt động end-to-end."""
    return "pong"


async def process_document(ctx: dict[str, Any], doc_id: str) -> str | None:
    async with get_sessionmaker()() as session:
        status = await _process_document(
            session, ctx["settings"], uuid.UUID(doc_id), ctx["provider"], ctx["fallback"]
        )
    return status.value if status else None


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["provider"], ctx["fallback"] = build_providers(settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["provider"].aclose()
    if ctx["fallback"]:
        await ctx["fallback"].aclose()


_settings = get_settings()


class WorkerSettings:
    functions = [ping, process_document]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = _settings.worker_concurrency
    job_timeout = _settings.job_timeout_s
    max_tries = 1  # lỗi provider đã retry bên trong; không chạy lại cả document (tốn tiền)
    health_check_interval = 30
