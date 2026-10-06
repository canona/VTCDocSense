"""CLI quản trị. Ví dụ: `python -m app.cli check`, `python -m app.cli extract file.pdf`."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import get_args

from redis.asyncio import Redis

from app.core import readiness
from app.core.config import ProviderName, get_settings
from app.core.logging import setup_logging
from app.pipeline.export import write_outputs
from app.pipeline.pdf import PdfError
from app.pipeline.run import process_pdf
from app.providers import ProviderError, build_provider


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


async def _extract(files: list[Path], out: Path | None, provider_name: ProviderName | None) -> int:
    settings = get_settings()
    provider = build_provider(settings, provider_name)
    fallback = (
        build_provider(settings, settings.fallback_provider)
        if settings.fallback_enabled and settings.fallback_provider
        else None
    )
    failed = 0
    try:
        for f in files:
            try:
                gp = await process_pdf(f.read_bytes(), f.name, provider, settings, fallback)
            except (PdfError, ProviderError, OSError) as e:
                failed += 1
                print(f"[LỖI] {f}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            paths = write_outputs(gp, out or f.parent, f.stem, folder=f.parent.name)
            status = "CẦN RÀ SOÁT" if gp.needs_review else "OK"
            print(
                f"[{status}] {f.name}: {gp.loai_van_ban.value} | số {gp.so_gp.value} | "
                f"{gp.meta.duration_ms} ms | {gp.meta.input_tokens}+{gp.meta.output_tokens} token"
            )
            for r in gp.review_reasons:
                print(f"    - {r}")
            for p in paths:
                print(f"    -> {p}")
    finally:
        await provider.aclose()
        if fallback:
            await fallback.aclose()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Kiểm tra kết nối DB/Redis/worker/provider")
    ex = sub.add_parser("extract", help="Bóc tách 1..N file PDF -> <tên>.json + <tên>.xlsx")
    ex.add_argument("files", nargs="+", type=Path)
    ex.add_argument("--out", type=Path, default=None, help="Thư mục output (mặc định: cạnh file PDF)")
    ex.add_argument("--provider", choices=get_args(ProviderName), default=None, help="Ghi đè LLM_PROVIDER")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return asyncio.run(_check())
    if args.cmd == "extract":
        setup_logging(get_settings().log_level)
        return asyncio.run(_extract(args.files, args.out, args.provider))
    return 2


if __name__ == "__main__":
    sys.exit(main())
