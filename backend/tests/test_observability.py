from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from app.observability import LangfuseClient, _timestamp


def test_langfuse_trace_is_disabled_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.observability.settings.langfuse_base_url", "")
    monkeypatch.setattr("app.observability.settings.langfuse_public_key", "")
    monkeypatch.setattr("app.observability.settings.langfuse_secret_key", "")
    client = LangfuseClient()
    assert client.enabled is False
    asyncio.run(
        client.trace_chat(
            name="chat",
            input_text="input",
            output_text="output",
            metadata={},
        )
    )


def test_langfuse_trace_posts_trace_and_generation_and_swallows_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.observability.settings.langfuse_base_url", "https://langfuse.invalid/")
    monkeypatch.setattr("app.observability.settings.langfuse_public_key", "public")
    monkeypatch.setattr("app.observability.settings.langfuse_secret_key", "secret")
    calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object], headers: dict[str, str]) -> None:
            calls.append((url, json, headers))

    monkeypatch.setattr("app.observability.httpx.AsyncClient", FakeAsyncClient)
    client = LangfuseClient()
    asyncio.run(
        client.trace_chat(
            name="/chat",
            input_text="question",
            output_text="answer",
            metadata={"model": "model-a", "scenario_id": "default"},
            usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        )
    )
    assert client.enabled is True
    assert calls[0][0] == "https://langfuse.invalid/api/public/ingestion"
    assert [item["type"] for item in calls[0][1]["batch"]] == ["trace-create", "generation-create"]
    expected_auth = base64.b64encode(b"public:secret").decode("ascii")
    assert calls[0][2]["Authorization"] == f"Basic {expected_auth}"
    assert _timestamp().endswith("Z")

    class FailingAsyncClient(FakeAsyncClient):
        async def post(self, *args, **kwargs) -> None:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("app.observability.httpx.AsyncClient", FailingAsyncClient)
    asyncio.run(
        client.trace_chat(
            name="/chat",
            input_text="question",
            output_text=None,
            metadata={"model": "model-a"},
        )
    )
