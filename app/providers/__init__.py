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
            response_format=settings.openai_compat_response_format,
            model_filter=settings.openai_compat_model_filter,
        )
    if name == "router":
        from app.providers.openai_compat import OpenAICompatProvider

        if not settings.router_base_url:
            raise ValueError("router cần ROUTER_BASE_URL")
        rkey = settings.router_api_key
        return OpenAICompatProvider(
            settings.router_base_url,
            settings.router_model,
            api_key=rkey.get_secret_value() if rkey else None,
            timeout_s=settings.provider_timeout_s,
            response_format=settings.openai_compat_response_format,
            name="router",
            model_filter=settings.router_model_filter,
        )
    if name == "anthropic":
        from app.providers.anthropic import AnthropicProvider

        if not settings.anthropic_api_key:
            raise ValueError("anthropic cần ANTHROPIC_API_KEY")
        return AnthropicProvider(
            settings.anthropic_api_key.get_secret_value(),
            settings.anthropic_model,
            timeout_s=settings.provider_timeout_s,
        )
    raise ValueError(f"Provider không hỗ trợ: {name}")
