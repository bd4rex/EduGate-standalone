from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app import chat_service
from app.knowledge import KnowledgeHit
from app.schemas import ChatRequest, ModelCatalogItem, ModelProviderConnectionRequest, TeachingScenario
from app.security import StudentIdentity


def _request(content: str = "Explain fractions") -> ChatRequest:
    return ChatRequest(messages=[{"role": "user", "content": content}])


def test_chat_payload_includes_teacher_prompt_knowledge_hits_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = KnowledgeHit(
        source_id="math",
        file_id="lesson-file",
        filename="fractions.txt",
        chunk_index=0,
        text="A numerator is above the denominator.",
        score=2.0,
    )
    monkeypatch.setattr(chat_service.state.knowledge_store, "search", lambda *args, **kwargs: [hit])
    scenario = TeachingScenario(
        model="math-model",
        system_prompt="Teach with questions.",
        temperature=0.2,
        max_tokens=300,
        knowledge_source_id="math",
        knowledge_strict=True,
    )

    payload = chat_service._build_chat_payload(_request(), scenario=scenario, stream=True)

    assert payload["model"] == "math-model"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 300
    assert payload["messages"][0]["content"] == "Teach with questions."
    assert "fractions.txt" in payload["messages"][1]["content"]
    assert "Strict mode" in payload["messages"][1]["content"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("RELATED", True), ("UNRELATED", False), ("probably related", True)],
)
def test_llm_topic_gate_understands_related_and_unrelated_responses(
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    expected: bool,
) -> None:
    scenario = TeachingScenario(model="topic-model", knowledge_source_id="math", knowledge_strict=True)
    monkeypatch.setattr(chat_service, "_knowledge_source_summary", lambda _: "Fractions lesson")

    async def complete(payload: dict[str, Any]) -> dict[str, Any]:
        assert payload["temperature"] == 0
        assert "Fractions lesson" in payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": answer}}]}

    monkeypatch.setattr(chat_service, "_chat_completion", complete)
    assert asyncio.run(chat_service._llm_topic_related(_request(), scenario)) is expected


def test_llm_topic_gate_fails_closed_when_the_probe_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = TeachingScenario(model="topic-model", knowledge_source_id="math", knowledge_strict=True)
    monkeypatch.setattr(chat_service, "_knowledge_source_summary", lambda _: "Fractions lesson")

    async def fail(payload: dict[str, Any]) -> dict[str, Any]:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(chat_service, "_chat_completion", fail)
    assert asyncio.run(chat_service._llm_topic_related(_request(), scenario)) is False


def test_knowledge_source_summary_handles_missing_empty_and_indexed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        mode = "indexed"

        def get_source(self, source_id: str):
            if self.mode == "missing":
                raise HTTPException(status_code=404, detail="missing")
            return SimpleNamespace(id=source_id, name="Mathematics")

        def list_files(self, source_id: str):
            if self.mode == "empty":
                return []
            return [SimpleNamespace(filename="fractions.txt", chunk_count=3)]

    store = FakeStore()
    monkeypatch.setattr(chat_service.state, "knowledge_store", store)
    assert chat_service._knowledge_source_summary(None) == "No knowledge source is mounted."
    store.mode = "missing"
    assert "does not exist" in chat_service._knowledge_source_summary("math")
    store.mode = "empty"
    assert "no searchable files" in chat_service._knowledge_source_summary("math")
    store.mode = "indexed"
    summary = chat_service._knowledge_source_summary("math")
    assert "Mathematics" in summary and "fractions.txt (3 chunks)" in summary


def test_provider_key_reuse_and_discovery_errors_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ModelCatalogItem(
        id="saved-model",
        name="Saved Model",
        provider="Provider",
        provider_id="provider-id",
        upstream_model_id="native-model",
        base_url="https://provider.invalid/v1",
        credential_id="model:saved-model",
    )
    monkeypatch.setattr(
        chat_service.state,
        "runtime_config",
        SimpleNamespace(data=SimpleNamespace(model_catalog={model.id: model})),
    )
    monkeypatch.setattr(
        chat_service.state,
        "secret_store",
        SimpleNamespace(get=lambda key: "saved-key" if key == "model:saved-model" else None),
    )
    explicit = ModelProviderConnectionRequest(
        provider="Provider",
        provider_id="provider-id",
        base_url="https://provider.invalid/v1",
        api_key="explicit-key",
    )
    assert chat_service._provider_api_key(explicit) == ("explicit-key", False)
    reuse = explicit.model_copy(update={"api_key": None, "credential_model_id": model.id})
    assert chat_service._provider_api_key(reuse) == ("saved-key", True)
    missing = reuse.model_copy(update={"credential_model_id": "missing"})
    with pytest.raises(HTTPException) as missing_error:
        chat_service._provider_api_key(missing)
    assert missing_error.value.status_code == 404

    class FailingClient:
        error: Exception | None = None

        async def list_openai_models(self, **kwargs):
            if self.error:
                raise self.error
            return [{"id": "model-a"}]

    failing = FailingClient()
    monkeypatch.setattr(chat_service.state, "client", failing)
    assert asyncio.run(chat_service._discover_provider_models(explicit))[0] == [{"id": "model-a"}]

    request = httpx.Request("GET", "https://provider.invalid/v1/models")
    failing.error = httpx.HTTPStatusError(
        "bad gateway",
        request=request,
        response=httpx.Response(503, request=request, text="unavailable"),
    )
    with pytest.raises(HTTPException) as status_error:
        asyncio.run(chat_service._discover_provider_models(explicit))
    assert status_error.value.status_code == 502
    failing.error = httpx.ReadTimeout("slow")
    with pytest.raises(HTTPException) as timeout_error:
        asyncio.run(chat_service._discover_provider_models(explicit))
    assert timeout_error.value.status_code == 504
    failing.error = httpx.ConnectError("offline")
    with pytest.raises(HTTPException) as connection_error:
        asyncio.run(chat_service._discover_provider_models(explicit))
    assert connection_error.value.status_code == 502


def test_stream_routing_covers_direct_missing_key_and_gateway_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct = ModelCatalogItem(
        id="local-model",
        name="Local Model",
        provider="Provider",
        upstream_model_id="native-model",
        base_url="https://provider.invalid/v1",
        credential_id="model:local-model",
    )

    class FakeClient:
        async def stream_openai_chat_completion(self, **kwargs):
            assert kwargs["payload"]["model"] == "native-model"
            yield b"direct"

        async def stream_chat_completion(self, payload):
            yield b"gateway"

    monkeypatch.setattr(chat_service.state, "client", FakeClient())
    monkeypatch.setattr(chat_service, "_direct_openai_model", lambda model_id: direct)
    monkeypatch.setattr(chat_service.state.secret_store, "get", lambda key: "secret")

    async def collect(payload: dict[str, Any]) -> bytes:
        return b"".join([chunk async for chunk in chat_service._stream_chat_completion(payload)])

    assert asyncio.run(collect({"model": direct.id})) == b"direct"
    monkeypatch.setattr(chat_service.state.secret_store, "get", lambda key: None)
    assert b"event: error" in asyncio.run(collect({"model": direct.id}))
    monkeypatch.setattr(chat_service, "_direct_openai_model", lambda model_id: None)
    assert asyncio.run(collect({"model": "gateway-model"})) == b"gateway"


def test_stream_helpers_cover_sync_sources_connection_errors_and_missing_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect_sync() -> bytes:
        return b"".join([chunk async for chunk in chat_service._iterate_stream_bytes([b"a", b"b"])])

    assert asyncio.run(collect_sync()) == b"ab"

    async def connection_failure(payload: dict[str, Any]):
        raise httpx.ConnectError("offline")
        yield b""

    monkeypatch.setattr(chat_service, "_stream_chat_completion", connection_failure)

    async def collect_error() -> bytes:
        return b"".join([chunk async for chunk in chat_service._stream_with_errors({"model": "x"})])

    converted = asyncio.run(collect_error())
    assert b"event: error" in converted and b"502" in converted

    logs: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(chat_service.state.business_db, "log_request", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(chat_service, "_record_classroom_turn", lambda **kwargs: records.append(kwargs))
    scenario = TeachingScenario(model="stream-model")

    async def consume() -> bytes:
        stream = chat_service._stream_with_completion_log(
            [b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
            route="/chat/stream",
            request=_request(),
            scenario=scenario,
            effective_scenario_id="default",
            teacher_id="admin",
            student=StudentIdentity(student_id="student", computer_name="PC", client_ip="127.0.0.1"),
        )
        return b"".join([chunk async for chunk in stream])

    assert b"partial" in asyncio.run(consume())
    assert logs[-1]["stream_done"] is False
    assert logs[-1]["stream_finish_reason"] == "ended_without_done"
    assert records[-1]["output_content"] == "partial"
