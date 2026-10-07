"""Giới hạn request/phút theo API key: cửa sổ cố định 60 giây trên Redis (INCR + EXPIRE)."""

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)


@dataclass
class RateResult:
    allowed: bool
    limit: int
    remaining: int
    reset_s: int  # số giây tới khi cửa sổ mới bắt đầu


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int) -> RateResult: ...


def _window() -> tuple[int, int]:
    now = int(time.time())
    return now // 60, 60 - now % 60


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def hit(self, key: str, limit: int) -> RateResult:
        window, reset = _window()
        name = f"rl:{key}:{window}"
        try:
            async with self.redis.pipeline(transaction=True) as p:
                p.incr(name)
                p.expire(name, 61)
                count, _ = await p.execute()
        except (RedisError, OSError) as e:
            # Redis tạm lỗi: cho qua (không chặn đối tác vì hạ tầng), ghi cảnh báo
            log.warning("rate limit: Redis lỗi, bỏ qua giới hạn", extra={"error": type(e).__name__})
            return RateResult(True, limit, limit, reset)
        return RateResult(count <= limit, limit, max(0, limit - count), reset)


class MemoryRateLimiter:
    """Cho test/script chạy trong 1 tiến trình (không cần Redis)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def hit(self, key: str, limit: int) -> RateResult:
        window, reset = _window()
        name = f"{key}:{window}"
        self.counts[name] = self.counts.get(name, 0) + 1
        count = self.counts[name]
        return RateResult(count <= limit, limit, max(0, limit - count), reset)
