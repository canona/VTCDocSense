"""Provider giả lập cho dev/test: không gọi mạng.

Mặc định trả output hợp lệ tối thiểu theo `schema_name` (phân loại -> KHAC) để chạy thử pipeline;
test truyền `response` (mọi lần gọi) hoặc `responses` (theo schema_name).
"""

import time
from typing import Any

from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult

_DEFAULTS: dict[str, dict[str, Any]] = {
    "PhanLoai": {"loai_van_ban": "KHAC", "ten_loai_giay_phep": None, "ly_do": "mock provider"},
    "GiayPhep": {"loai_van_ban": "KHAC"},
}


class MockProvider(ExtractionProvider):
    name = "mock"

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.model = "mock"
        self.response = response
        self.responses = responses or {}
        self.calls: list[ExtractionRequest] = []

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        start = time.perf_counter()
        self.calls.append(req)
        if self.response is not None:
            data = self.response
        else:
            data = self.responses.get(req.schema_name, _DEFAULTS.get(req.schema_name, {}))
        return ExtractionResult(
            data=dict(data),
            provider=self.name,
            model=self.model,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def health(self) -> bool:
        return True
