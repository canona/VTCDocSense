"""Ước tính token/chi phí trước khi chạy (upload, chạy lại). Không gọi LLM.

Hệ số đo từ lần record 2026-10-07 (Gemini, 3 GP Báo Bình Thuận, ảnh 150 DPI xám):
- PDF có lớp chữ: ~1.000 token/trang text + ~1.550 token ảnh trang 1 + ~1.500 token prompt cố định,
  output ~2.400; phân loại bằng rule (không gọi model).
- PDF scan: ~1.850 token/trang ảnh + ~1.300 prompt, output ~1.700; phân loại ~2.400 in / 100 out.
Escalate (trường trọng yếu low) có thể gấp đôi phần bóc tách -> báo cả mức tối đa.
"""

from dataclasses import dataclass

from app.core.config import Settings
from app.llm.pricing import Pricing

TEXT_PAGE_IN = 1000
TEXT_FIXED_IN = 1500 + 1550
TEXT_OUT = 2400
SCAN_PAGE_IN = 1850
SCAN_FIXED_IN = 1300
SCAN_OUT = 1700
CLASSIFY_IN, CLASSIFY_OUT = 2400, 100


@dataclass
class Estimate:
    documents: int = 0
    text_pages: int = 0
    scan_pages: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_vnd: float | None = None  # None: chưa có đơn giá trong config/pricing.toml
    max_cost_vnd: float | None = None
    can_escalate: bool = True  # có MODEL_VISION khác model bóc tách ban đầu

    @property
    def max_calls(self) -> int:
        return self.calls + (self.documents if self.can_escalate else 0)  # tối đa 1 lần escalate/document


def _provider_model(settings: Settings) -> str | None:
    """Model mặc định của provider chính (khi không đặt MODEL_TEXT/MODEL_VISION)."""
    return {
        "router": settings.router_model,
        "gemini": settings.gemini_model,
        "openai": settings.openai_model,
        "openai_compat": settings.openai_compat_model,
        "anthropic": settings.anthropic_model,
    }.get(settings.llm_provider)


def _first(models: str | None) -> str | None:
    return models.split(",")[0].strip() if models else None


def estimate(settings: Settings, docs: list[tuple[int | None, str | None]]) -> Estimate:
    """docs: [(số trang, pdf_type TEXT|SCAN|MIXED)]. MIXED tính như scan (an toàn về chi phí)."""
    est = Estimate(documents=len(docs))
    pricing = Pricing.load(settings.pricing_file)
    provider = settings.llm_provider
    default_model = _first(_provider_model(settings))
    text_model = _first(settings.model_text) or default_model
    vision_model = _first(settings.model_vision) or default_model
    est.can_escalate = bool(settings.model_vision) and settings.model_vision != settings.model_text
    cost: float | None = 0.0
    extract_cost: float | None = 0.0

    def add(model: str | None, tin: int, tout: int, *, extract: bool) -> None:
        nonlocal cost, extract_cost
        est.calls += 1
        est.input_tokens += tin
        est.output_tokens += tout
        price = pricing.get(model, provider) if model else None
        if price is None:
            cost = extract_cost = None
            return
        c = price.cost(tin, tout)
        if cost is not None:
            cost += c
        if extract and extract_cost is not None:
            extract_cost += c

    for pages, kind in docs:
        n = pages or 1
        if kind == "TEXT":
            est.text_pages += n
            add(text_model, TEXT_FIXED_IN + n * TEXT_PAGE_IN, TEXT_OUT, extract=True)
        else:
            est.scan_pages += n
            add(text_model, CLASSIFY_IN, CLASSIFY_OUT, extract=False)
            add(vision_model, SCAN_FIXED_IN + n * SCAN_PAGE_IN, SCAN_OUT, extract=True)
    if cost is not None and extract_cost is not None:
        est.cost_vnd = round(cost, 1)
        est.max_cost_vnd = round(cost + (extract_cost if est.can_escalate else 0), 1)
    return est
