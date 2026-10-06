"""Các kiểm tra phụ thuộc dùng cho /readyz và CLI `check`."""

import asyncio
from collections.abc import Awaitable

from arq.constants import default_queue_name, health_check_key_suffix
from redis.asyncio import Redis
from sqlalchemy import text

from app.db.session import get_engine
from app.providers import ExtractionProvider

CHECK_TIMEOUT_S = 5.0


async def check_db() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis(redis: Redis) -> None:
    await redis.ping()


async def check_worker(redis: Redis) -> None:
    # arq ghi heartbeat định kỳ vào key này (TTL ~ health_check_interval)
    if not await redis.exists(default_queue_name + health_check_key_suffix):
        raise RuntimeError("không thấy heartbeat của worker")


async def check_provider(provider: ExtractionProvider) -> None:
    if not await provider.health():
        raise RuntimeError(f"provider {provider.name} không sẵn sàng")


async def _run(coro: Awaitable[None]) -> str:
    try:
        await asyncio.wait_for(coro, CHECK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - trả lỗi dạng chuỗi cho client
        return f"error: {type(e).__name__}: {e}"[:300]
    return "ok"


async def run_checks(redis: Redis, provider: ExtractionProvider) -> dict[str, str]:
    names = ["db", "redis", "worker", "provider"]
    results = await asyncio.gather(
        _run(check_db()),
        _run(check_redis(redis)),
        _run(check_worker(redis)),
        _run(check_provider(provider)),
    )
    return dict(zip(names, results, strict=True))
