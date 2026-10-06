"""Cấu hình arq worker. Chạy: `arq app.worker.settings.WorkerSettings`."""

from typing import Any

from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.providers import build_provider


async def ping(ctx: dict[str, Any]) -> str:
    """Task kiểm tra worker hoạt động end-to-end."""
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ctx["settings"] = settings
    ctx["provider"] = build_provider(settings)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["provider"].aclose()


_settings = get_settings()


class WorkerSettings:
    functions = [ping]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    max_jobs = _settings.worker_concurrency
    job_timeout = _settings.job_timeout_s
    health_check_interval = 30
