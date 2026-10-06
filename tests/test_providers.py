import json

import httpx
import pytest

from app.core.config import Settings
from app.providers import ExtractionRequest, PageInput, ProviderError, build_provider
from app.providers.mock import MockProvider
from app.providers.openai_compat import OpenAICompatProvider, VLLMProvider


def _req() -> ExtractionRequest:
    return ExtractionRequest(
        system_prompt="sys",
        user_prompt="user",
        pages=[PageInput(page_no=1, text="GIẤY PHÉP"), PageInput(page_no=2, image_png=b"\x89PNG")],
        json_schema={"type": "object"},
    )


def test_factory_selects_provider() -> None:
    assert isinstance(build_provider(Settings(llm_provider="mock")), MockProvider)
    assert isinstance(build_provider(Settings(llm_provider="vllm")), VLLMProvider)
    with pytest.raises(ValueError):
        build_provider(Settings(llm_provider="openai_compat"))


async def test_mock_provider() -> None:
    p = MockProvider({"so_gp": "1/GP-BTTTT"})
    res = await p.extract(_req())
    assert res.data == {"so_gp": "1/GP-BTTTT"}
    assert len(p.calls) == 1


def _provider_with(handler: httpx.MockTransport) -> OpenAICompatProvider:
    p = OpenAICompatProvider("http://x/v1", "m")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=handler)
    return p


async def test_openai_compat_parses_json_and_usage() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"a": 1}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    res = await _provider_with(httpx.MockTransport(handler)).extract(_req())
    assert res.data == {"a": 1}
    assert (res.input_tokens, res.output_tokens) == (10, 3)
    assert seen["response_format"]["type"] == "json_schema"  # type: ignore[index]
    content = seen["messages"][1]["content"]  # type: ignore[index]
    assert any(part["type"] == "image_url" for part in content)


async def test_openai_compat_errors() -> None:
    bad_json = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    )
    with pytest.raises(ProviderError) as e:
        await _provider_with(bad_json).extract(_req())
    assert e.value.retryable

    bad_req = httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))
    with pytest.raises(ProviderError) as e:
        await _provider_with(bad_req).extract(_req())
    assert not e.value.retryable


def test_empty_env_values_are_none() -> None:
    s = Settings(fallback_provider="", openai_compat_base_url="", anthropic_api_key="")  # type: ignore[arg-type]
    assert s.fallback_provider is None
    assert s.openai_compat_base_url is None
    assert s.anthropic_api_key is None
