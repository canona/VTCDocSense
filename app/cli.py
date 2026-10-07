"""CLI quản trị.

python -m app.cli check
python -m app.cli extract a.pdf b.pdf                 # mock / cache, không gọi API thật
python -m app.cli extract a.pdf --live [--record]     # gọi API thật (tốn tiền), ghi fixture
python -m app.cli cache stats|clear
python -m app.cli cost                                # chi phí API thật hôm nay
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import get_args

from redis.asyncio import Redis

from app.core import readiness
from app.core.config import ProviderName, Settings, get_settings
from app.core.logging import setup_logging
from app.llm.metered import MeteredProvider, RunState
from app.llm.store import DiskCache, Ledger
from app.pipeline.export import write_outputs
from app.pipeline.pdf import PdfError
from app.pipeline.run import process_pdf
from app.providers import ProviderError, build_provider, build_providers


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


async def _extract(
    settings: Settings, files: list[Path], out: Path | None, provider_name: ProviderName | None, record: bool
) -> int:
    inner, inner_fb = build_providers(settings, provider_name)
    run = RunState()  # LLM_MAX_CALLS_PER_RUN tính cho cả lệnh
    provider = MeteredProvider(inner, settings, run=run, record=record)
    fallback = MeteredProvider(inner_fb, settings, run=run, record=record) if inner_fb else None
    failed = 0
    try:
        for f in files:
            provider.record_label = f"{f.parent.name}/{f.name}"
            if fallback:
                fallback.record_label = provider.record_label
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
    live = [r for r in run.records if r.live and r.error is None]
    hits = sum(r.cache_hit for r in run.records)
    print(
        f"LLM: {len(live)} lượt gọi thật, {hits} cache hit, "
        f"{sum(r.input_tokens for r in live)}+{sum(r.output_tokens for r in live)} token, "
        f"~{run.cost_vnd:,.1f} đ | hôm nay {Ledger(settings.ledger_dir).spent():,.1f}"
        f"/{settings.llm_daily_budget_vnd:,.0f} đ"
    )
    return 1 if failed else 0


def _cache(action: str) -> int:
    cache = DiskCache(get_settings().cache_dir)
    if action == "clear":
        print(f"Đã xóa {cache.clear()} mục cache tại {cache.root}")
    else:
        n, size = cache.stats()
        print(f"{n} mục, {size / 1024:.1f} KB tại {cache.root}")
    return 0


def _cost() -> int:
    settings = get_settings()
    entries = Ledger(settings.ledger_dir).entries()
    by_model: dict[str, list[float]] = {}
    for e in entries:
        agg = by_model.setdefault(e.model, [0, 0, 0, 0.0])
        agg[0] += 1
        agg[1] += e.input_tokens
        agg[2] += e.output_tokens
        agg[3] += e.cost_vnd
    for model, (n, i, o, c) in by_model.items():
        print(f"{model}: {int(n)} lượt, {int(i)}+{int(o)} token, {c:,.1f} đ")
    total = sum(e.cost_vnd for e in entries)
    print(f"Tổng hôm nay: {total:,.1f} đ / ngân sách {settings.llm_daily_budget_vnd:,.0f} đ")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Kiểm tra kết nối DB/Redis/worker/provider")
    ex = sub.add_parser("extract", help="Bóc tách 1..N file PDF -> <tên>.json + <tên>.xlsx")
    ex.add_argument("files", nargs="+", type=Path)
    ex.add_argument("--out", type=Path, default=None, help="Thư mục output (mặc định: cạnh file PDF)")
    ex.add_argument("--provider", choices=get_args(ProviderName), default=None, help="Ghi đè LLM_PROVIDER")
    ex.add_argument("--live", action="store_true", help="Cho phép gọi API LLM thật (tốn tiền)")
    ex.add_argument("--record", action="store_true", help="Ghi phản hồi thành fixture cho mock replay")
    ca = sub.add_parser("cache", help="Cache phản hồi LLM")
    ca.add_argument("action", choices=["stats", "clear"])
    sub.add_parser("cost", help="Chi phí gọi API thật hôm nay (theo sổ chi phí)")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return asyncio.run(_check())
    if args.cmd == "extract":
        settings = get_settings()
        if args.live:
            settings = settings.model_copy(update={"allow_live_llm": True})
        setup_logging(settings.log_level)
        return asyncio.run(_extract(settings, args.files, args.out, args.provider, args.record))
    if args.cmd == "cache":
        return _cache(args.action)
    if args.cmd == "cost":
        return _cost()
    return 2


if __name__ == "__main__":
    sys.exit(main())
