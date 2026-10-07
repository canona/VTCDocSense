"""Provider giả lập cho dev/test: không gọi mạng.

Thứ tự: `response` (mọi lần gọi) -> fixture đã ghi bằng `--record` (theo digest request, trong
`fixtures_dir`) -> `responses` (theo schema_name) -> output hợp lệ tối thiểu (phân loại -> KHAC).
"""

import time
from pathlib import Path
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
        fixtures_dir: Path | None = None,
    ) -> None:
        self.model = "mock"
        self.response = response
        self.responses = responses or {}
        self.fixtures_dir = fixtures_dir
        self.calls: list[ExtractionRequest] = []
        self.replayed = 0

    def _fixture(self, req: ExtractionRequest) -> dict[str, Any] | None:
        if self.fixtures_dir is None:
            return None
        from app.llm.store import Fixtures, request_digest

        rec = Fixtures(self.fixtures_dir).load(request_digest(req), req.schema_name)
        return rec["data"] if rec else None

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        start = time.perf_counter()
        self.calls.append(req)
        if self.response is not None:
            data = self.response
        elif (fx := self._fixture(req)) is not None:
            data = fx
            self.replayed += 1
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
