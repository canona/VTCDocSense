"""Provider cho endpoint OpenAI-compatible: vLLM, OpenAI, Gemini (/v1beta/openai), 9router, Ollama...

Model: một tên, danh sách theo thứ tự ưu tiên (`a,b,c`), hoặc `auto` (lấy từ `GET /models`).
Gặp 429 (hết quota) / 404 (model không còn) / 503 (quá tải) -> tạm khóa model đó và chuyển model kế tiếp.
Chỉ khi mọi model đều bị khóa mới ném `ProviderError` (không retry nếu thời gian chờ dài).

`response_format`:
- `json_schema` (mặc định): structured output strict.
- `json_object`: chỉ ép JSON, schema đưa vào prompt (cho endpoint không nhận json_schema strict).
Nếu endpoint trả 400 cho json_schema, provider tự chuyển sang json_object cho các lần gọi sau.
"""

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any, Literal

import httpx

from app.providers.base import ExtractionProvider, ExtractionRequest, ExtractionResult, ProviderError

log = logging.getLogger(__name__)

ResponseFormat = Literal["json_schema", "json_object"]

AUTO = "auto"
AUTO_MAX_MODELS = 8
# Model không dùng cho đọc văn bản/ảnh -> bỏ khi `auto`
_AUTO_EXCLUDE = re.compile(
    r"embed|tts|audio|image|imagen|veo|live|aqa|robotics|computer-use|customtools|banana|whisper|dall-e"
    r"|moderation|rerank|transcribe|realtime|search",
    re.IGNORECASE,
)
# Thời gian khóa model mặc định khi không đọc được thời gian chờ từ response
COOLDOWN_429_S = 60.0
COOLDOWN_503_S = 30.0
COOLDOWN_404_S = 24 * 3600.0
# Mọi model đều bị khóa lâu hơn ngưỡng này -> không retry (vd hết quota ngày)
MAX_RETRYABLE_WAIT_S = 60.0


def parse_models(value: str | None) -> list[str]:
    return [m.strip() for m in (value or "").split(",") if m.strip()]


def _supports_vision(model_info: dict[str, Any]) -> bool:
    """Endpoint có metadata (vd 9router `capabilities.vision`) -> bỏ model không đọc được ảnh."""
    caps = model_info.get("capabilities")
    return not (isinstance(caps, dict) and caps.get("vision") is False)


def rank_models(ids: list[str], pattern: str | None = None) -> list[str]:
    """Lọc + xếp model cho `auto`: bản ổn định trước, flash > pro > lite, phiên bản mới trước."""
    rx = re.compile(pattern, re.IGNORECASE) if pattern else None
    names = {i.removeprefix("models/") for i in ids}
    cands = [n for n in names if not _AUTO_EXCLUDE.search(n) and (rx is None or rx.search(n))]

    def key(n: str) -> tuple[int, int, tuple[int, ...], str]:
        low = n.lower()
        unstable = int(any(t in low for t in ("preview", "exp", "latest")))
        tier = 2 if "lite" in low else 0 if "flash" in low else 1 if "pro" in low else 3
        m = re.search(r"(\d+(?:\.\d+)*)", n)
        version = tuple(-int(x) for x in m.group(1).split(".")) if m else ()
        return unstable, tier, version, n

    return sorted(cands, key=key)[:AUTO_MAX_MODELS]


_DUR_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?")


def retry_after_s(resp: httpx.Response) -> float | None:
    """Đọc thời gian chờ: header Retry-After, `retryDelay` (Gemini) hoặc 'retry in 10h45m43s'."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    text = resp.text
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', text)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", text, re.IGNORECASE)
    if m and m.group(1):
        d = _DUR_RE.fullmatch(m.group(1))
        if d:
            h, mi, s = d.groups()
            return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    return None


class OpenAICompatProvider(ExtractionProvider):
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        response_format: ResponseFormat = "json_schema",
        *,
        name: str | None = None,
        model_filter: str | None = None,
    ) -> None:
        if name:
            self.name = name
        self.models = parse_models(model) or [AUTO]
        self.model = self.models[0]  # model đang dùng gần nhất (cho log/meta)
        self.model_filter = model_filter
        self.response_format: ResponseFormat = response_format
        self._cooldown: dict[str, float] = {}  # model -> thời điểm (monotonic) được dùng lại
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_s)

    # ---------- chọn model ----------

    async def _resolve_auto(self) -> list[str]:
        try:
            resp = await self._client.get("/models", timeout=15)
        except httpx.HTTPError as e:
            raise ProviderError(f"không lấy được danh sách model: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"GET /models HTTP {resp.status_code}: {resp.text[:300]}", retryable=False)
        ids = [str(m["id"]) for m in resp.json().get("data", []) if m.get("id") and _supports_vision(m)]
        ranked = rank_models(ids, self.model_filter)
        if not ranked:
            raise ProviderError("auto: không có model phù hợp trong GET /models", retryable=False)
        log.info("auto chọn model", extra={"provider": self.name, "models": ranked})
        return ranked

    async def candidate_models(self) -> list[str]:
        """Thay mục `auto` trong danh sách (vd "ocr,auto") bằng các model lấy từ GET /models."""
        if AUTO in self.models:
            auto = await self._resolve_auto()
            resolved: list[str] = []
            for m in self.models:
                for x in auto if m == AUTO else [m]:
                    if x not in resolved:
                        resolved.append(x)
            self.models = resolved
        return self.models

    def _available(self, models: list[str]) -> list[str]:
        now = time.monotonic()
        return [m for m in models if self._cooldown.get(m, 0.0) <= now]

    async def _wait_for_model(self, models: list[str]) -> list[str]:
        """Không còn model nào: nếu model sớm nhất mở khóa trong <= MAX_RETRYABLE_WAIT_S thì chờ."""
        available = self._available(models)
        if available:
            return available
        wait = min(self._cooldown.get(m, 0.0) for m in models) - time.monotonic()
        if wait <= MAX_RETRYABLE_WAIT_S:
            log.info("chờ model mở khóa", extra={"provider": self.name, "seconds": round(wait, 1)})
            await asyncio.sleep(max(wait, 0.0))
            return self._available(models)
        return []

    def _block(self, model: str, seconds: float, reason: str) -> None:
        self._cooldown[model] = time.monotonic() + seconds
        log.warning(
            "tạm khóa model, chuyển model kế tiếp",
            extra={"provider": self.name, "model": model, "seconds": round(seconds), "reason": reason},
        )

    # ---------- request ----------

    def _build_messages(self, req: ExtractionRequest, fmt: ResponseFormat) -> list[dict[str, Any]]:
        user_prompt = req.user_prompt
        if fmt == "json_object":
            schema = json.dumps(req.json_schema, ensure_ascii=False)
            keys = ", ".join(req.json_schema.get("properties", {}))
            user_prompt += (
                "\n\nChỉ trả về một JSON object hợp lệ theo JSON Schema sau. "
                f"Dùng ĐÚNG tên khóa cấp cao nhất: {keys}.\n{schema}"
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for page in req.pages:
            if page.text:
                content.append(
                    {"type": "text", "text": f"--- Trang {page.page_no} (text layer) ---\n{page.text}"}
                )
            if page.image:
                b64 = base64.b64encode(page.image).decode()
                content.append({"type": "text", "text": f"--- Trang {page.page_no} (ảnh) ---"})
                content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{page.image_mime};base64,{b64}"}}
                )
        return [
            {"role": "system", "content": req.system_prompt},
            {"role": "user", "content": content},
        ]

    def _body(self, req: ExtractionRequest, fmt: ResponseFormat, model: str) -> dict[str, Any]:
        rf: dict[str, Any]
        if fmt == "json_schema":
            rf = {
                "type": "json_schema",
                "json_schema": {"name": req.schema_name, "schema": req.json_schema, "strict": True},
            }
        else:
            rf = {"type": "json_object"}
        return {
            "model": model,
            "messages": self._build_messages(req, fmt),
            "temperature": 0,
            "stream": False,  # 9router mặc định trả SSE nếu không ghi rõ
            "response_format": rf,
        }

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as e:
            raise ProviderError(f"timeout: {e}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"http error: {e}") from e

    async def _call_model(self, req: ExtractionRequest, model: str) -> httpx.Response:
        resp = await self._post(self._body(req, self.response_format, model))
        if resp.status_code == 400 and self.response_format == "json_schema":
            log.warning(
                "endpoint từ chối json_schema, chuyển sang json_object",
                extra={"provider": self.name, "model": model, "detail": resp.text[:300]},
            )
            self.response_format = "json_object"
            resp = await self._post(self._body(req, "json_object", model))
        return resp

    async def extract(self, req: ExtractionRequest) -> ExtractionResult:
        models = await self.candidate_models()
        start = time.perf_counter()
        last_error = "không có model khả dụng"
        filtered = False  # có model chặn nội dung (vd Gemini RECITATION)
        payload: Any = None
        raw: str | None = None
        for model in await self._wait_for_model(models):
            resp = await self._call_model(req, model)
            status = resp.status_code
            if status == 429:
                self._block(model, retry_after_s(resp) or COOLDOWN_429_S, "429 hết quota/rate limit")
            elif status == 404:
                self._block(model, COOLDOWN_404_S, "404 model không tồn tại/không còn cung cấp")
            elif status in (401, 403):
                # Qua router: từng model có thể dùng tài khoản/provider khác nhau -> thử model kế tiếp
                self._block(model, COOLDOWN_404_S, f"{status} không có quyền dùng model")
            elif status in (502, 503, 529):
                self._block(model, retry_after_s(resp) or COOLDOWN_503_S, f"{status} quá tải")
            elif status >= 400:
                # 4xx khác thường là lỗi request -> không retry
                raise ProviderError(f"{model}: HTTP {status}: {resp.text[:500]}", retryable=status >= 500)
            else:
                payload = _json_or_error(resp, model)
                raw = _message_content(payload)
                if raw is not None and _loads(raw) is None and self.response_format == "json_schema":
                    # Endpoint (vd combo của router) bỏ qua response_format -> đưa schema vào prompt
                    log.warning(
                        "model trả văn bản không phải JSON, chuyển sang json_object",
                        extra={"provider": self.name, "model": model},
                    )
                    self.response_format = "json_object"
                    resp = await self._post(self._body(req, "json_object", model))
                    if resp.status_code == 200:
                        payload = _json_or_error(resp, model)
                        raw = _message_content(payload)
                if raw is not None:
                    self.model = model
                    break
                reason = _finish_reason(payload)
                if "content_filter" in reason or "recitation" in reason.lower() or "safety" in reason.lower():
                    # Chặn theo nội dung request: không khóa model, chỉ thử model khác cho request này
                    filtered = True
                    log.warning(
                        "model chặn nội dung, thử model kế tiếp",
                        extra={"provider": self.name, "model": model, "finish_reason": reason},
                    )
                    last_error = f"{model}: bị chặn ({reason})"
                    continue
                raise ProviderError(f"{model}: response không có nội dung (finish_reason={reason})")
            last_error = f"{model}: HTTP {status}: {resp.text[:300]}"
        if raw is None:
            if filtered:
                # Chạy lại cùng model gần như chắc chắn bị chặn tiếp -> để provider phụ xử lý
                raise ProviderError(f"{self.name}: mọi model đều chặn/khóa ({last_error})", retryable=False)
            raise ProviderError(
                f"{self.name}: mọi model đều tạm khóa ({last_error})", retryable=self._retryable()
            )
        try:
            data = json.loads(_strip_fence(raw))
        except json.JSONDecodeError as e:
            raise ProviderError(f"invalid JSON from model {self.model}: {e}") from e
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

    def _retryable(self) -> bool:
        """Đáng retry nếu có model sắp được mở khóa (vd 503 thoáng qua), không đáng nếu hết quota ngày."""
        if not self._cooldown:
            return True
        soonest = min(self._cooldown.values()) - time.monotonic()
        return soonest <= MAX_RETRYABLE_WAIT_S

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def aclose(self) -> None:
        await self._client.aclose()


def _message_content(payload: Any) -> str | None:
    """Lấy `choices[0].message.content`; một số endpoint trả list hoặc dict lồng (vd Gemini compat)."""
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices") or []
    message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # dạng [{"type": "text", "text": "..."}]
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) and content.strip() else None


def _json_or_error(resp: httpx.Response, model: str) -> Any:
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        ctype = resp.headers.get("content-type", "")
        raise ProviderError(f"{model}: response không phải JSON ({ctype}): {resp.text[:200]}") from e


def _loads(raw: str) -> Any:
    try:
        return json.loads(_strip_fence(raw))
    except json.JSONDecodeError:
        return None


def _finish_reason(payload: Any) -> str:
    if isinstance(payload, list) and payload:
        payload = payload[0]
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _strip_fence(raw: str) -> str:
    """Một số model bọc JSON trong ```json ... ``` khi ở chế độ json_object."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s


class VLLMProvider(OpenAICompatProvider):
    name = "vllm"
