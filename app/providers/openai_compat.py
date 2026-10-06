"""Provider cho endpoint OpenAI-compatible (vLLM, hoặc bất kỳ server tương thích)."""

import base64
import json
import time
from typing import Any

import httpx

from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError


class OpenAICompatProvider(ExtractionProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout_s: float = 120.0):
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    def _build_messages(self, req: ExtractionRequest) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": req.user_prompt}]
        for page in req.pages:
            if page.text:
                content.append(
                    {"type": "text", "text": f"--- Trang {page.page_no} (text layer) ---\n{page.text}"}
                )
            if page.image_png:
                b64 = base64.b64encode(page.image_png).decode()
                content.append({"type": "text", "text": f"--- Trang {page.page_no} (ảnh) ---"})
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        return [
            {"role": "system", "content": req.system_prompt},
            {"role": "user", "content": content},
        ]

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        body = {
            "model": self.model,
            "messages": self._build_messages(req),
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "schema": req.json_schema, "strict": True},
            },
        }
        start = time.perf_counter()
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"timeout: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"http error: {e}") from e
        if resp.status_code >= 400:
            # 4xx (trừ 429) thường là lỗi request -> không retry
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:500]}", retryable=retryable)

        payload = resp.json()
        raw = payload["choices"][0]["message"]["content"] or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProviderError(f"invalid JSON from model: {e}") from e
        usage = payload.get("usage") or {}
        return ExtractionResult(
            data=data,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            duration_ms=int((time.perf_counter() - start) * 1000),
            raw_text=raw,
        )

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()


class VLLMProvider(OpenAICompatProvider):
    name = "vllm"
