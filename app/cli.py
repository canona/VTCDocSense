"""CLI quản trị. Ví dụ: `python -m app.cli check`."""

import argparse
import asyncio
import json
import sys

from redis.asyncio import Redis

from app.core import readiness
from app.core.config import get_settings
from app.providers import build_provider


async def _check() -> int:
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    provider = build_provider(settings)
    try:
        checks = await readiness.run_checks(redis, provider)
    finally:
        await provider.aclose()
        await redis.aclose()
    print(json.dumps({"provider": settings.llm_provider, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if all(v == "ok" for v in checks.values()) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Kiểm tra kết nối DB/Redis/worker/provider")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return asyncio.run(_check())
    return 2


if __name__ == "__main__":
    sys.exit(main())
