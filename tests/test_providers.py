import json

import httpx
import pytest

from app.core.config import Settings
from app.providers import ExtractionRequest, PageInput, ProviderError, build_provider
from app.providers.anthropic import AnthropicProvider
from app.providers.mock import MockProvider
from app.providers.openai_compat import (
    OpenAICompatProvider,
    VLLMProvider,
    parse_models,
    rank_models,
    retry_after_s,
)


def _req() -> ExtractionRequest:
    return ExtractionRequest(
        system_prompt="sys",
        user_prompt="user",
        pages=[PageInput(page_no=1, text="GIẤY PHÉP"), PageInput(page_no=2, image=b"\x89PNG")],
        json_schema={"type": "object"},
    )


def test_factory_selects_provider() -> None:
    assert isinstance(build_provider(Settings(llm_provider="mock")), MockProvider)
    assert isinstance(build_provider(Settings(llm_provider="vllm")), VLLMProvider)
    with pytest.raises(ValueError):
        build_provider(Settings(llm_provider="openai_compat"))
    with pytest.raises(ValueError):
        build_provider(Settings(llm_provider="anthropic", anthropic_api_key=""))  # type: ignore[arg-type]
    assert isinstance(
        build_provider(Settings(llm_provider="anthropic", anthropic_api_key="k")),  # type: ignore[arg-type]
        AnthropicProvider,
    )


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


async def test_openai_compat_downgrades_to_json_object_on_400() -> None:
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        formats.append(body["response_format"]["type"])
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, text="schema not supported")
        assert "JSON Schema" in body["messages"][1]["content"][0]["text"]
        fenced = '```json\n{"a": 2}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    p = _provider_with(httpx.MockTransport(handler))
    assert (await p.extract(_req())).data == {"a": 2}
    assert (await p.extract(_req())).data == {"a": 2}
    assert formats == ["json_schema", "json_object", "json_object"]


async def test_anthropic_tool_use() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["x-api-key"] == "k"
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": "GiayPhep", "input": {"so_gp": "1/GP-CBC"}}],
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
        )

    p = AnthropicProvider("k", "claude-test")
    p._client = httpx.AsyncClient(
        base_url="http://x", headers={"x-api-key": "k"}, transport=httpx.MockTransport(handler)
    )
    res = await p.extract(_req())
    assert res.data == {"so_gp": "1/GP-CBC"}
    assert (res.input_tokens, res.output_tokens) == (7, 2)
    assert seen["tool_choice"] == {"type": "tool", "name": "GiayPhep"}
    content = seen["messages"][0]["content"]  # type: ignore[index]
    assert any(part["type"] == "image" for part in content)

    no_tool = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"content": [], "stop_reason": "max_tokens"})
    )
    p._client = httpx.AsyncClient(base_url="http://x", transport=no_tool)
    with pytest.raises(ProviderError):
        await p.extract(_req())


def _gemini_429(delay: str = "39s") -> httpx.Response:
    return httpx.Response(
        429,
        json=[
            {
                "error": {
                    "code": 429,
                    "message": f"Quota exceeded. Please retry in {delay}.",
                    "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "39s"}],
                }
            }
        ],
    )


def _ok(content: str = '{"a": 1}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_parse_models_and_retry_after() -> None:
    assert parse_models(" a, b ,,c ") == ["a", "b", "c"]
    assert retry_after_s(httpx.Response(429, headers={"retry-after": "12"})) == 12
    assert retry_after_s(_gemini_429()) == 39
    assert (
        retry_after_s(httpx.Response(429, text="Please retry in 10h45m43.03s."))
        == 10 * 3600 + 45 * 60 + 43.03
    )
    assert retry_after_s(httpx.Response(429, text="nope")) is None


def test_rank_models_for_auto() -> None:
    ids = [
        "models/gemini-2.5-flash",
        "models/gemini-3.8-flash",
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.1-pro-preview",
        "models/gemini-3.6-flash",
        "models/gemini-2.5-flash-preview-tts",
        "models/gemini-3-pro-image",
        "models/text-embedding-004",
        "models/gemini-flash-latest",
        "models/gemini-3.5-pro",
    ]
    ranked = rank_models(ids)
    assert ranked[:3] == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash"]
    assert ranked.index("gemini-3.5-pro") < ranked.index("gemini-3.5-flash-lite")
    assert ranked[-2:] == ["gemini-flash-latest", "gemini-3.1-pro-preview"]  # preview/latest cuối
    assert not any("tts" in m or "image" in m or "embedding" in m for m in ranked)
    assert rank_models(["gpt-x", "gemini-3.8-flash"], pattern="gemini") == ["gemini-3.8-flash"]


async def test_failover_to_next_model_on_quota_and_remembers_block() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return {"m1": _gemini_429(), "m2": httpx.Response(404, text="no longer available")}.get(model, _ok())

    p = OpenAICompatProvider("http://x/v1", "m1,m2,m3")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    res = await p.extract(_req())
    assert res.model == "m3" and res.data == {"a": 1}
    assert seen == ["m1", "m2", "m3"]
    await p.extract(_req())
    assert seen[3:] == ["m3"]  # m1, m2 đang bị khóa -> không gọi lại


async def test_all_models_exhausted_long_wait_is_not_retryable() -> None:
    p = OpenAICompatProvider("http://x/v1", "m1,m2")
    p._client = httpx.AsyncClient(
        base_url="http://x/v1",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, text="Please retry in 10h45m43s.")),
    )
    with pytest.raises(ProviderError) as e:
        await p.extract(_req())
    assert not e.value.retryable
    assert "mọi model" in str(e.value)


async def test_auto_resolves_models_from_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"data": [{"id": "models/gemini-3.8-flash"}, {"id": "models/imagen-4"}]}
            )
        assert json.loads(request.content)["model"] == "gemini-3.8-flash"
        return _ok()

    p = OpenAICompatProvider("http://x/v1", "auto")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    res = await p.extract(_req())
    assert res.model == "gemini-3.8-flash"
    assert p.models == ["gemini-3.8-flash"]


def test_router_provider_factory() -> None:
    p = build_provider(
        Settings(llm_provider="router", router_base_url="https://9router.example/v1", router_api_key="k")  # type: ignore[arg-type]
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p.name == "router" and p.models == ["auto"]
    with pytest.raises(ValueError):
        build_provider(Settings(llm_provider="router", router_base_url=""))  # type: ignore[arg-type]


def _recitation() -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"finish_reason": "content_filter: RECITATION", "message": {"role": "assistant"}}]},
    )


async def test_content_filter_tries_next_model_without_blocking() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        return _recitation() if model == "m1" else _ok()

    p = OpenAICompatProvider("http://x/v1", "m1,m2")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert (await p.extract(_req())).model == "m2"
    await p.extract(_req())
    assert seen == ["m1", "m2", "m1", "m2"]  # m1 không bị khóa (chặn theo nội dung từng request)


async def test_all_models_filtered_is_not_retryable() -> None:
    p = OpenAICompatProvider("http://x/v1", "m1,m2")
    p._client = httpx.AsyncClient(
        base_url="http://x/v1", transport=httpx.MockTransport(lambda r: _recitation())
    )
    with pytest.raises(ProviderError) as e:
        await p.extract(_req())
    assert not e.value.retryable
    assert "RECITATION" in str(e.value)


async def test_auto_inside_list_and_vision_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "ocr", "owned_by": "combo"},
                        {"id": "gemini/gemini-3.8-flash", "capabilities": {"vision": True}},
                        {"id": "openrouter/text-only:free", "capabilities": {"vision": False}},
                    ]
                },
            )
        return _ok()

    p = OpenAICompatProvider("http://x/v1", "ocr,auto")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert await p.candidate_models() == ["ocr", "gemini/gemini-3.8-flash"]


async def test_forbidden_model_moves_to_next() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(403, text="FreeTierError") if model == "ocr" else _ok()

    p = OpenAICompatProvider("http://x/v1", "ocr,m2")
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert (await p.extract(_req())).model == "m2"


async def test_plain_text_reply_switches_to_json_object() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert body["stream"] is False
        if body["response_format"]["type"] == "json_schema":
            return _ok("Văn bản này là giấy phép mở chuyên trang ...")  # router bỏ qua response_format
        return _ok('{"a": 3}')

    p = _provider_with(httpx.MockTransport(handler))
    assert (await p.extract(_req())).data == {"a": 3}
    assert [b["response_format"]["type"] for b in bodies] == ["json_schema", "json_object"]  # type: ignore[index]


async def test_openai_compat_drops_reasoning_effort_on_400() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, text="Unknown parameter: reasoning_effort")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {}})

    p = _provider_with(httpx.MockTransport(handler))
    p.reasoning_effort = "low"
    assert (await p.extract(_req())).data == {"a": 1}
    assert [("reasoning_effort" in b) for b in bodies] == [True, False]
    assert bodies[1]["response_format"]["type"] == "json_schema"  # type: ignore[index]  # không hạ cấp nhầm
