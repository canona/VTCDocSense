"""Cấu hình arq worker. Chạy: `arq app.worker.settings.WorkerSettings`."""

import uuid
from typing import Any

import httpx
from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_sessionmaker
from app.providers import build_providers
from app.services import retention, webhooks
from app.services.documents import process_document as _process_document


async def ping(ctx: dict[str, Any]) -> str:
    """Task kiểm tra worker hoạt động end-to-end."""
    return "pong"


async def process_document(ctx: dict[str, Any], doc_id: str, read_cache: bool = True) -> str | None:
    async with get_sessionmaker()() as session:
        status = await _process_document(
            session,
            ctx["settings"],
            uuid.UUID(doc_id),
            ctx["provider"],
            ctx["fallback"],
            read_cache=read_cache,
        )
        # Gửi webhook của document này ngay (không chờ cron 10 giây); thất bại -> cron retry theo backoff
        await webhooks.deliver_due(
            session, ctx["settings"], ctx["http"], document_id=uuid.UUID(doc_id), limit=5
        )
    return status.value if status else None


async def send_webhooks(ctx: dict[str, Any]) -> int:
    async with get_sessionmaker()() as session:
        return await webhooks.deliver_due(session, ctx["settings"], ctx["http"])


async def purge_expired(ctx: dict[str, Any]) -> dict[str, int]:
    async with get_sessionmaker()() as session:
        return (await retention.purge(session, ctx["settings"])).__dict__


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["provider"], ctx["fallback"] = build_providers(settings)
    # Không theo redirect (tránh bị dẫn sang địa chỉ nội bộ sau khi đã kiểm tra URL)
    ctx["http"] = httpx.AsyncClient(follow_redirects=False)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["provider"].aclose()
    if ctx["fallback"]:
        await ctx["fallback"].aclose()


_settings = get_settings()


class WorkerSettings:
    functions = [ping, process_document]
    cron_jobs = [
        cron(send_webhooks, second={0, 10, 20, 30, 40, 50}, run_at_startup=True, timeout=300),
        cron(purge_expired, hour={19}, minute={30}, run_at_startup=True),  # 02:30 giờ VN
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = _settings.worker_concurrency
    job_timeout = _settings.job_timeout_s
    max_tries = 1  # lỗi provider đã retry bên trong; không chạy lại cả document (tốn tiền)
    health_check_interval = 30
