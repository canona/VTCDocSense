"""Provider Claude (Messages API) qua httpx; structured output bằng tool bắt buộc gọi."""

import base64
import time
from typing import Any

import httpx

from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError

API_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class AnthropicProvider(ExtractionProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout_s: float = 120.0, max_tokens: int = 8192) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=API_URL,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION},
            timeout=timeout_s,
        )

    def _content(self, req: ExtractionRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": req.user_prompt}]
        for page in req.pages:
            if page.text:
                content.append(
                    {"type": "text", "text": f"--- Trang {page.page_no} (text layer) ---\n{page.text}"}
                )
            if page.image:
                content.append({"type": "text", "text": f"--- Trang {page.page_no} (ảnh) ---"})
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": page.image_mime,
                            "data": base64.b64encode(page.image).decode(),
                        },
                    }
                )
        return content

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        model = req.model or self.model
        body = {
            "model": model,
            "max_tokens": req.max_tokens or self.max_tokens,
            "temperature": 0,
            # Prompt caching: tools (schema) + system là phần cố định, cache tới hết system
            "system": [{"type": "text", "text": req.system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": self._content(req)}],
            "tools": [
                {
                    "name": req.schema_name,
                    "description": "Ghi kết quả bóc tách theo đúng schema.",
                    "input_schema": req.json_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": req.schema_name},
        }
        start = time.perf_counter()
        try:
            resp = await self._client.post("/v1/messages", json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"timeout: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"http error: {e}") from e
        if resp.status_code >= 400:
            retryable = resp.status_code in (429, 529) or resp.status_code >= 500
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:500]}", retryable=retryable)

        payload = resp.json()
        tool = next((b for b in payload.get("content", []) if b.get("type") == "tool_use"), None)
        if tool is None or not isinstance(tool.get("input"), dict):
            raise ProviderError(f"model không trả tool_use (stop_reason={payload.get('stop_reason')})")
        usage = payload.get("usage") or {}
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_write = usage.get("cache_creation_input_tokens") or 0
        return ExtractionResult(
            data=tool["input"],
            provider=self.name,
            model=model,
            input_tokens=usage.get("input_tokens", 0) + cache_read + cache_write,
            cached_input_tokens=cache_read,
            output_tokens=usage.get("output_tokens", 0),
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/v1/models", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()
