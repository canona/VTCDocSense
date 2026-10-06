from app.core.config import ProviderName, Settings
from app.providers.base import (
    ExtractionProvider,
    ExtractionRequest,
    ExtractionResult,
    PageInput,
    ProviderError,
)

__all__ = [
    "ExtractionProvider",
    "ExtractionRequest",
    "ExtractionResult",
    "PageInput",
    "ProviderError",
    "build_provider",
]


def build_provider(settings: Settings, name: ProviderName | None = None) -> ExtractionProvider:
    name = name or settings.llm_provider
    if name == "mock":
        from app.providers.mock import MockProvider

        return MockProvider()
    if name == "vllm":
        from app.providers.openai_compat import VLLMProvider

        return VLLMProvider(
            settings.vllm_base_url, settings.model_name, timeout_s=settings.provider_timeout_s
        )
    if name == "openai_compat":
        from app.providers.openai_compat import OpenAICompatProvider

        if not settings.openai_compat_base_url or not settings.openai_compat_model:
            raise ValueError("openai_compat cần OPENAI_COMPAT_BASE_URL và OPENAI_COMPAT_MODEL")
        key = settings.openai_compat_api_key
        return OpenAICompatProvider(
            settings.openai_compat_base_url,
            settings.openai_compat_model,
            api_key=key.get_secret_value() if key else None,
            timeout_s=settings.provider_timeout_s,
        )
    if name == "anthropic":
        raise NotImplementedError("Provider anthropic sẽ được triển khai ở M1")
    raise ValueError(f"Provider không hỗ trợ: {name}")
