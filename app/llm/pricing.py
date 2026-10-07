"""Đơn giá token đọc từ `config/pricing.toml` (stdlib tomllib)."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

PER = 1_000_000


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cached_input: float | None = None

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        cached = min(cached_input_tokens, input_tokens)
        cached_rate = self.input if self.cached_input is None else self.cached_input
        return (
            (input_tokens - cached) * self.input + cached * cached_rate + output_tokens * self.output
        ) / PER


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

        def parse(d: dict[str, float]) -> Price:
            return Price(float(d["input"]), float(d["output"]), d.get("cached_input"))

        models = {name: parse(v) for name, v in (raw.get("models") or {}).items()}
        default = parse(raw["default"]) if "default" in raw else None
        return cls(models, default)

    def get(self, model: str) -> Price | None:
        model = model.removeprefix("models/")
        if model in self.models:
            return self.models[model]
        prefixes = [k for k in self.models if model.startswith(k)]
        if prefixes:
            return self.models[max(prefixes, key=len)]
        return self.default

    def require(self, models: list[str]) -> None:
        """Ném PricingError nếu có model (hoặc 'auto') chưa có đơn giá."""
        missing = [m for m in models if self.get(m) is None]
        if missing:
            raise PricingError(
                f"Chưa có đơn giá cho model {', '.join(missing)} trong config/pricing.toml "
                '(thêm [models."<tên>"] hoặc [default]) - từ chối gọi API (không kiểm soát được chi phí)'
            )

    def cost(self, model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        price = self.get(model)
        return price.cost(input_tokens, output_tokens, cached_input_tokens) if price else 0.0
