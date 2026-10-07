"""Đơn giá token đọc từ `config/pricing.toml` (stdlib tomllib). Mọi chi phí nội bộ tính bằng VND (đồng)."""

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PER = 1_000_000


@dataclass(frozen=True)
class Price:
    """Giá đồng / 1 triệu token; `min_per_request`: phí tối thiểu mỗi request (đồng)."""

    input: float
    output: float
    cached_input: float | None = None
    min_per_request: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        cached = min(cached_input_tokens, input_tokens)
        cached_rate = self.input if self.cached_input is None else self.cached_input
        c = ((input_tokens - cached) * self.input + cached * cached_rate + output_tokens * self.output) / PER
        return max(c, self.min_per_request)


class PricingError(Exception):
    pass


class Pricing:
    def __init__(self, models: dict[str, Price], default: Price | None = None) -> None:
        self.models = models
        self.default = default

    @classmethod
    def load(cls, path: Path) -> "Pricing":
        if not path.exists():
            return cls({})
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        vnd_per_usd = raw.get("vnd_per_usd")

        def parse(name: str, d: dict[str, Any]) -> Price | None:
            currency = str(d.get("currency", "VND")).upper()
            if currency == "VND":
                rate = 1.0
            elif currency == "USD" and vnd_per_usd:
                rate = float(vnd_per_usd)  # giá USD -> đồng
            else:
                log.warning(
                    "bỏ qua đơn giá (giá USD cần vnd_per_usd, hoặc tiền tệ lạ)", extra={"model": name}
                )
                return None
            cached = d.get("cached_input")
            return Price(
                float(d["input"]) * rate,
                float(d["output"]) * rate,
                None if cached is None else float(cached) * rate,
                float(d.get("min_per_request", 0)) * rate,
            )

        models = {name: p for name, v in (raw.get("models") or {}).items() if (p := parse(name, v))}
        default = parse("default", raw["default"]) if "default" in raw else None
        return cls(models, default)

    def get(self, model: str, provider: str | None = None) -> Price | None:
        """Thứ tự: "<provider>/<model>" (giá riêng theo nhà cung cấp) -> "<model>" -> tiền tố dài nhất."""
        model = model.removeprefix("models/")
        keys = ([f"{provider}/{model}"] if provider else []) + [model]
        for k in keys:
            if k in self.models:
                return self.models[k]
        for k in keys:
            prefixes = [p for p in self.models if k.startswith(p)]
            if prefixes:
                return self.models[max(prefixes, key=len)]
        return self.default

    def require(self, models: list[str], provider: str | None = None) -> None:
        """Ném PricingError nếu có model (hoặc 'auto') chưa có đơn giá."""
        missing = [m for m in models if self.get(m, provider) is None]
        if missing:
            raise PricingError(
                f"Chưa có đơn giá cho model {', '.join(missing)} trong config/pricing.toml "
                '(thêm [models."<tên>"] hoặc [default]) - từ chối gọi API (không kiểm soát được chi phí)'
            )

    def cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        provider: str | None = None,
    ) -> float:
        price = self.get(model, provider)
        return price.cost(input_tokens, output_tokens, cached_input_tokens) if price else 0.0
