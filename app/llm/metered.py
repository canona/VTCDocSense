"""Bọc mọi provider: cache -> cờ live -> giới hạn lượt/ngân sách -> gọi -> ghi sổ/log/fixture.

Tạo 1 `MeteredProvider` cho mỗi run (1 lệnh CLI / 1 document) để đếm `LLM_MAX_CALLS_PER_RUN`;
provider bên trong (HTTP client) dùng chung.
"""

import logging
import time
from dataclasses import dataclass, field

from app.core.config import Settings
from app.llm.pricing import Pricing, PricingError
from app.llm.store import DiskCache, Fixtures, Ledger, LedgerEntry, cache_key, request_digest
from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError

log = logging.getLogger(__name__)


class LiveCallBlocked(ProviderError):
    """Từ chối gọi API thật (thiếu cờ live / vượt ngân sách / thiếu đơn giá). Không retry."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


@dataclass
class CallRecord:
    provider: str
    model: str
    schema_name: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False
    live: bool = False
    duration_ms: int = 0
    error: str | None = None


@dataclass
class RunState:
    live_calls: int = 0
    records: list[CallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)


def is_live(provider: ExtractionProvider) -> bool:
    return provider.name != "mock"


def model_candidates(provider: ExtractionProvider, req: ExtractionRequest) -> list[str]:
    if req.model:
        return [m.strip() for m in req.model.split(",") if m.strip()]
    return list(getattr(provider, "models", None) or [provider.model])


class MeteredProvider(ExtractionProvider):
    def __init__(
        self,
        inner: ExtractionProvider,
        settings: Settings,
        *,
        run: RunState | None = None,
        record: bool = False,
        record_label: str | None = None,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.settings = settings
        self.run = run or RunState()
        self.cache = DiskCache(settings.cache_dir) if settings.llm_cache_enabled else None
        self.ledger = Ledger(settings.ledger_dir)
        self.pricing = Pricing.load(settings.pricing_file)
        self.fixtures = Fixtures(settings.llm_fixtures_dir) if record else None
        self.record_label = record_label

    def _guard(self, req: ExtractionRequest) -> None:
        s = self.settings
        if not s.allow_live_llm:
            raise LiveCallBlocked(
                f"Chặn gọi API LLM thật ({self.name}): đặt ALLOW_LIVE_LLM=1 hoặc chạy CLI với --live"
            )
        if self.run.live_calls >= s.llm_max_calls_per_run:
            raise LiveCallBlocked(
                f"Vượt LLM_MAX_CALLS_PER_RUN={s.llm_max_calls_per_run} lượt gọi trong run này"
            )
        spent = self.ledger.spent()
        if spent >= s.llm_daily_budget_usd:
            raise LiveCallBlocked(
                f"Vượt ngân sách ngày LLM_DAILY_BUDGET_USD={s.llm_daily_budget_usd}: đã dùng ${spent:.4f}"
            )
        try:
            self.pricing.require(model_candidates(self.inner, req))
        except PricingError as e:
            if s.llm_require_pricing:
                raise LiveCallBlocked(str(e)) from e
            log.warning("chưa có đơn giá, chi phí ghi 0", extra={"models": model_candidates(self.inner, req)})

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        live = is_live(self.inner)
        digest = request_digest(req)
        key = cache_key(digest, ",".join(model_candidates(self.inner, req)))
        if live and self.cache:
            hit = self.cache.get(key)
            if hit is not None:
                self.run.records.append(
                    CallRecord(hit.provider, hit.model, req.schema_name, cache_hit=True, live=False)
                )
                log.info("llm cache hit", extra={"model": hit.model, "schema": req.schema_name})
                return hit

        if live:
            self._guard(req)
            self.run.live_calls += 1
        start = time.perf_counter()
        try:
            res = await self.inner.extract(req)
        except ProviderError as e:
            if live and not e.billed:
                self.run.live_calls -= 1  # lỗi 4xx/5xx không bị tính phí -> không trừ lượt
            self.run.records.append(
                CallRecord(
                    self.name,
                    req.model or self.inner.model,
                    req.schema_name,
                    live=live,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    error=str(e)[:500],
                )
            )
            raise
        self.model = res.model
        cost = self.pricing.cost(res.model, res.input_tokens, res.output_tokens, res.cached_input_tokens)
        rec = CallRecord(
            res.provider,
            res.model,
            req.schema_name,
            res.input_tokens,
            res.output_tokens,
            res.cached_input_tokens,
            cost if live else 0.0,
            cache_hit=False,
            live=live,
            duration_ms=res.duration_ms,
        )
        self.run.records.append(rec)
        log.info(
            "llm call",
            extra={
                "provider": rec.provider,
                "model": rec.model,
                "schema": rec.schema_name,
                "in": rec.input_tokens,
                "out": rec.output_tokens,
                "cached": rec.cached_input_tokens,
                "cost_usd": round(rec.cost_usd, 6),
                "live": live,
            },
        )
        if live:
            self.ledger.add(
                LedgerEntry(
                    time.time(),
                    rec.provider,
                    rec.model,
                    rec.schema_name,
                    rec.input_tokens,
                    rec.output_tokens,
                    rec.cached_input_tokens,
                    rec.cost_usd,
                )
            )
            if self.cache:
                self.cache.put(key, res)
        if self.fixtures:
            path = self.fixtures.save(digest, req, res, self.record_label)
            log.info("đã ghi fixture", extra={"path": str(path)})
        return res

    async def health(self) -> bool:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()
