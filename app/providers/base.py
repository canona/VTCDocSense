from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class PageInput(BaseModel):
    """Một trang logic đưa vào model: text layer (nếu có) và/hoặc ảnh (PNG/JPEG)."""

    page_no: int
    text: str | None = None
    image: bytes | None = None
    image_mime: str = "image/png"
    # Định danh ổn định của ảnh (vd "sha256-pdf:1R") để làm khóa cache thay cho bytes ảnh
    # (bytes JPEG có thể khác nhau giữa các phiên bản Pillow)
    image_id: str | None = None


class ExtractionRequest(BaseModel):
    system_prompt: str
    user_prompt: str
    pages: list[PageInput]
    json_schema: dict[str, Any]
    schema_name: str = "GiayPhep"
    # Ghi đè model của provider (phân tầng MODEL_TEXT / MODEL_VISION); None = model mặc định
    model: str | None = None
    max_tokens: int | None = None


class ExtractionResult(BaseModel):
    data: dict[str, Any]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0  # phần input trúng prompt cache của nhà cung cấp
    duration_ms: int = 0
    raw_text: str | None = Field(default=None, repr=False)


class ProviderError(Exception):
    """Lỗi từ provider; `retryable` cho biết worker có nên thử lại không."""

    def __init__(self, message: str, *, retryable: bool = True, billed: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.billed = billed  # request đã được xử lý (HTTP 200) -> nhà cung cấp vẫn tính phí


class ExtractionProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def extract(self, req: ExtractionRequest) -> ExtractionResult: ...

    @abstractmethod
    async def health(self) -> bool: ...

    async def aclose(self) -> None:  # noqa: B027 - hook tùy chọn
        pass
