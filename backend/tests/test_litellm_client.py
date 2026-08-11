import asyncio
import json

import httpx
import pytest

from app.litellm_client import LiteLLMClient, _normalize_openai_models, _openai_url


def test_openai_url_accepts_base_or_full_chat_endpoint() -> None:
    assert _openai_url("https://api.example.com", "/chat/completions") == (
        "https://api.example.com/v1/chat/completions"
    )
    assert _openai_url("https://api.example.com/custom/v1", "/models") == (
        "https://api.example.com/custom/v1/models"
    )
    assert _openai_url(
        "https://api.example.com/custom/v1/chat/completions",
        "/chat/completions",
    ) == "https://api.example.com/custom/v1/chat/completions"
    assert _openai_url(
        "https://api.example.com/custom/v1/chat/completions",
        "/models",
    ) == "https://api.example.com/custom/v1/models"


def test_openai_model_list_is_normalized_sorted_and_deduplicated() -> None:
    assert _normalize_openai_models(
        {
            "data": [
                {"id": "qwen-plus", "owned_by": "aliyun"},
                {"model_name": "deepseek-v4-flash", "provider": "aliyun"},
                {"id": "qwen-plus", "owned_by": "updated-owner"},
                "glm-5",
                {"name": ""},
            ]
        }
    ) == [
        {"id": "deepseek-v4-flash", "owned_by": "aliyun"},
        {"id": "glm-5", "owned_by": ""},
        {"id": "qwen-plus", "owned_by": "updated-owner"},
    ]


def test_litellm_client_exercises_json_stream_and_openai_compatible_http_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        observed.append((request.method, request.url.path, dict(request.headers), payload))
        if request.url.path.endswith("/models"):
            if request.url.host == "upstream.test":
                return httpx.Response(
                    200,
                    json={"data": [{"id": "zeta"}, {"id": "alpha", "owned_by": "owner"}]},
                )
            return httpx.Response(200, json={"data": [{"id": "gateway-model"}]})
        if payload and payload.get("stream"):
            return httpx.Response(200, content=b"data: first\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def scenario() -> None:
        monkeypatch.setattr("app.litellm_client.settings.litellm_api_key", "gateway-key")
        client = LiteLLMClient()
        await client._client.aclose()
        await client._stream_client.aclose()
        transport = httpx.MockTransport(handler)
        client._client = httpx.AsyncClient(transport=transport)
        client._stream_client = httpx.AsyncClient(transport=transport)
        try:
            assert (await client.list_models())["data"][0]["id"] == "gateway-model"
            assert (await client.chat_completion({"model": "gateway-model"}))["choices"]
            gateway_stream = [chunk async for chunk in client.stream_chat_completion({"model": "gateway-model"})]
            assert b"[DONE]" in b"".join(gateway_stream)

            upstream = "https://upstream.test/custom"
            assert (await client.openai_chat_completion(
                base_url=upstream,
                api_key="upstream-key",
                payload={"model": "alpha"},
            ))["choices"]
            upstream_stream = [
                chunk
                async for chunk in client.stream_openai_chat_completion(
                    base_url=upstream,
                    api_key="upstream-key",
                    payload={"model": "alpha"},
                )
            ]
            assert b"first" in b"".join(upstream_stream)
            models = await client.list_openai_models(base_url=upstream, api_key="upstream-key")
            assert [item["id"] for item in models] == ["alpha", "zeta"]
            assert await client.probe_openai_provider(base_url=upstream, api_key="upstream-key") == {
                "ok": True,
                "model_count": 2,
            }
        finally:
            await client.close()

    asyncio.run(scenario())
    assert any(headers.get("authorization") == "Bearer gateway-key" for _, _, headers, _ in observed)
    assert any(headers.get("authorization") == "Bearer upstream-key" for _, _, headers, _ in observed)
    assert sum(1 for _, _, _, payload in observed if payload and payload.get("stream")) == 2


def test_litellm_client_raises_for_upstream_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="offline")

    async def scenario() -> None:
        client = LiteLLMClient()
        await client._client.aclose()
        await client._stream_client.aclose()
        transport = httpx.MockTransport(handler)
        client._client = httpx.AsyncClient(transport=transport)
        client._stream_client = httpx.AsyncClient(transport=transport)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat_completion({"model": "test"})
            with pytest.raises(httpx.HTTPStatusError):
                await client.list_openai_models(base_url="https://upstream.test", api_key="key")
        finally:
            await client.close()

    asyncio.run(scenario())
