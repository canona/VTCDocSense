"""Gọi provider có retry + fallback, ép kết quả về Pydantic model."""

import asyncio
import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.models.schema import Confidence, GiayPhepCore
from app.pipeline.validate import iter_fields
from app.providers import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError

log = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: list[str] = field(default_factory=list)  # "provider/model" mỗi lần gọi thành công

    def add(self, res: ExtractionResult) -> None:
        self.input_tokens += res.input_tokens
        self.output_tokens += res.output_tokens
        self.calls.append(f"{res.provider}/{res.model}")


async def call_with_retries[M: BaseModel](
    provider: ExtractionProvider,
    req: ExtractionRequest,
    model_cls: type[M],
    *,
    retries: int,
    usage: Usage,
    backoff_s: float = 1.0,
) -> tuple[M, ExtractionResult]:
    """Thử 1 + `retries` lần; JSON sai schema cũng tính là lỗi có thể thử lại."""
    last: ProviderError | None = None
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(min(backoff_s * 2 ** (attempt - 1), 8.0))
        try:
            res = await provider.extract(req)
        except ProviderError as e:
            log.warning(
                "provider lỗi", extra={"provider": provider.name, "attempt": attempt, "error": str(e)}
            )
            if not e.retryable:
                raise
            last = e
            continue
        usage.add(res)
        try:
            return model_cls.model_validate(res.data), res
        except ValidationError as e:
            log.warning(
                "JSON sai schema",
                extra={"provider": provider.name, "attempt": attempt, "errors": e.errors()[:5]},
            )
            last = ProviderError(f"JSON không đúng schema: {e.error_count()} lỗi")
    assert last is not None
    raise last


def low_ratio(core: GiayPhepCore) -> float:
    filled = [f for _, f in iter_fields(core) if f.value]
    if not filled:
        return 1.0
    return sum(f.confidence == Confidence.low for f in filled) / len(filled)


async def extract_with_fallback[M: BaseModel](
    primary: ExtractionProvider,
    fallback: ExtractionProvider | None,
    req: ExtractionRequest,
    model_cls: type[M],
    *,
    retries: int,
    usage: Usage,
    low_conf_ratio: float = 1.0,
) -> tuple[M, ExtractionResult]:
    try:
        obj, res = await call_with_retries(primary, req, model_cls, retries=retries, usage=usage)
    except ProviderError:
        if fallback is None:
            raise
        log.warning("chuyển sang provider phụ", extra={"provider": fallback.name, "reason": "primary failed"})
        return await call_with_retries(fallback, req, model_cls, retries=retries, usage=usage)

    if fallback is not None and isinstance(obj, GiayPhepCore) and low_ratio(obj) > low_conf_ratio:
        log.warning("độ tin cậy thấp, thử provider phụ", extra={"provider": fallback.name})
        try:
            obj2, res2 = await call_with_retries(fallback, req, model_cls, retries=retries, usage=usage)
        except ProviderError:
            return obj, res
        assert isinstance(obj2, GiayPhepCore)
        if low_ratio(obj2) < low_ratio(obj):
            return obj2, res2
    return obj, res
