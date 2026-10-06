"""Provider giả lập cho dev/test: trả về dict rỗng hợp lệ, không gọi mạng."""

import time
from typing import Any

from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult


class MockProvider(ExtractionProvider):
    name = "mock"

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.model = "mock"
        self.response = response or {}
        self.calls: list[ExtractionRequest] = []

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        start = time.perf_counter()
        self.calls.append(req)
        return ExtractionResult(
            data=dict(self.response),
            provider=self.name,
            model=self.model,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def health(self) -> bool:
        return True
