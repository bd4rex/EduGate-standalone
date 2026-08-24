from __future__ import annotations

import asyncio
import codecs
import json
import logging
import time
from contextlib import suppress
from typing import Any, AsyncIterator, Iterable

import httpx
from fastapi import HTTPException, status

from app import state
from app.db import latest_user_preview, now_ms
from app.schemas import (
    ChatRequest,
    ModelBatchImportRequest,
    ModelCatalogItem,
    ModelCatalogPublicItem,
    ModelProviderConnectionRequest,
    TeachingScenario,
    _provider_catalog_id,
)
from app.security import StudentIdentity


STRICT_KNOWLEDGE_MISS_MESSAGE = (
    "\u6839\u636e\u6559\u5e08\u5f53\u524d\u6302\u8f7d\u7684\u77e5\u8bc6\u5e93\uff0c\u6211\u6ca1\u6709\u627e\u5230\u4e0e\u8fd9\u4e2a\u95ee\u9898\u76f8\u5173\u7684\u8bfe\u5802\u8d44\u6599\u4f9d\u636e\u3002"
    "\u4e25\u683c\u77e5\u8bc6\u5e93\u6a21\u5f0f\u4e0b\uff0c\u6211\u4e0d\u80fd\u4f7f\u7528\u77e5\u8bc6\u5e93\u4ee5\u5916\u7684\u5185\u5bb9\u7ee7\u7eed\u56de\u7b54\u3002"
    "\u8bf7\u56de\u5230\u672c\u8282\u8bfe\u7684\u77e5\u8bc6\u70b9\u63d0\u95ee\uff0c\u6216\u8bf7\u8001\u5e08\u8865\u5145\u76f8\u5173\u8d44\u6599\u5230\u77e5\u8bc6\u5e93\u3002"
)

KNOWLEDGE_OVERVIEW_KEYWORDS = (
    "\u77e5\u8bc6\u5e93",
    "\u8d44\u6599\u5e93",
    "\u6709\u54ea\u4e9b\u5185\u5bb9",
    "\u6709\u4ec0\u4e48\u5185\u5bb9",
    "\u90fd\u6709\u4ec0\u4e48",
    "\u90fd\u6709\u54ea\u4e9b",
    "\u5f53\u524d\u8d44\u6599",
    "\u6302\u8f7d",
    "knowledge",
    "source",
    "materials",
    "files",
    "zhishiku",
)

GREETING_PATTERNS = (
    "hi",
    "hello",
    "\u4f60\u597d",
    "\u60a8\u597d",
    "\u65e9\u4e0a\u597d",
    "\u4e0b\u5348\u597d",
    "\u665a\u4e0a\u597d",
    "\u5728\u5417",
    "\u8c22\u8c22",
    "\u611f\u8c22",
    "ok",
    "\u597d\u7684",
    "\u597d",
    "\u55ef",
    "\u662f\u7684",
    "yes",
    "thanks",
    "thank you",
)

EXACT_GREETING_PATTERNS = {"ok", "yes", "\u597d\u7684", "\u597d", "\u55ef", "\u662f\u7684"}

STRICT_GREETING_MESSAGE = (
    "\u4f60\u597d\uff0c\u6211\u5728\u3002\u4f60\u53ef\u4ee5\u56f4\u7ed5\u5f53\u524d\u8bfe\u5802\u77e5\u8bc6\u5e93\u91cc\u7684\u5185\u5bb9\u63d0\u95ee\uff1b"
    "\u5982\u679c\u95ee\u9898\u8d85\u51fa\u8d44\u6599\u8303\u56f4\uff0c\u6211\u4f1a\u63d0\u9192\u4f60\u56de\u5230\u672c\u8282\u8bfe\u7684\u77e5\u8bc6\u70b9\u3002"
)

APPRECIATION_PATTERNS = (
    "\u8c22\u8c22",
    "\u611f\u8c22",
    "\u591a\u8c22",
    "\u8f9b\u82e6\u4e86",
    "\u4e0d\u9519",
    "\u771f\u4e0d\u9519",
    "\u592a\u597d\u4e86",
    "\u5f88\u597d",
    "\u660e\u767d\u4e86",
    "\u61c2\u4e86",
    "thanks",
    "thank you",
    "good",
    "great",
    "nice",
    "understood",
)

STRICT_APPRECIATION_MESSAGE = (
    "\u4e0d\u5ba2\u6c14\uff0c\u5f88\u9ad8\u5174\u80fd\u5e2e\u5230\u4f60\u3002\u4f60\u53ef\u4ee5\u7ee7\u7eed\u56f4\u7ed5\u672c\u8282\u8bfe\u5185\u5bb9\u63d0\u95ee\uff0c"
    "\u6211\u4f1a\u5c3d\u91cf\u7ed3\u5408\u5f53\u524d\u77e5\u8bc6\u5e93\u5e2e\u4f60\u68b3\u7406\u3002"
)

def _resolve_chat_context(request: ChatRequest) -> tuple[str, TeachingScenario, dict[str, Any] | None]:
    scenario_id = request.scenario_id
    scenario = state.runtime_config.get_scenario(scenario_id)
    teacher = state.business_db.get_teacher(state.settings.admin_username)
    if not scenario.ai_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="AI service is disabled for the current classroom",
        )
    return scenario_id, scenario, teacher


def _build_chat_payload(
    request: ChatRequest,
    *,
    scenario: TeachingScenario | None = None,
    stream: bool = False,
    strict_topic_related: bool = False,
) -> dict[str, Any]:
    if scenario is None:
        _, scenario, _ = _resolve_chat_context(request)
    messages = [message.model_dump() for message in request.messages]
    if scenario.system_prompt.strip():
        messages.insert(0, {"role": "system", "content": scenario.system_prompt})
    knowledge_context = _build_knowledge_context(request, scenario, strict_topic_related=strict_topic_related)
    if knowledge_context:
        insert_at = 1 if messages and messages[0].get("role") == "system" else 0
        messages.insert(insert_at, {"role": "system", "content": knowledge_context})

    payload: dict[str, Any] = {
        "model": scenario.model,
        "messages": messages,
        "temperature": scenario.temperature,
        "stream": stream,
    }
    if scenario.max_tokens is not None:
        payload["max_tokens"] = scenario.max_tokens
    return payload


def _knowledge_hits(request: ChatRequest, scenario: TeachingScenario):
    if not scenario.knowledge_source_id:
        return []
    latest_user_message = next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )
    return state.knowledge_store.search(
        scenario.knowledge_source_id,
        latest_user_message,
        limit=state.settings.knowledge_search_limit,
    )


def _is_strict_no_hit(request: ChatRequest, scenario: TeachingScenario) -> bool:
    return bool(scenario.knowledge_strict and scenario.knowledge_source_id and not _knowledge_hits(request, scenario))


async def _llm_topic_related(request: ChatRequest, scenario: TeachingScenario) -> bool:
    profile = _knowledge_source_summary(scenario.knowledge_source_id)
    payload = {
        "model": scenario.model,
        "temperature": 0,
        "max_tokens": 8,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a classroom knowledge-base topic gate. Only decide whether "
                    "the student question is related to the current knowledge-base topic. "
                    "Output only RELATED or UNRELATED. Do not answer the student question."
                ),
            },
            {
                "role": "user",
                "content": f"Knowledge base summary:\n{profile}\n\nStudent question: {_latest_user_text(request)}",
            },
        ],
    }
    try:
        response = await _chat_completion(payload)
    except Exception:
        return False
    content = (
        response.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
        .upper()
    )
    if "UNRELATED" in content:
        return False
    return "RELATED" in content

async def _strict_miss_decision(request: ChatRequest, scenario: TeachingScenario) -> tuple[bool, bool]:
    if not _is_strict_no_hit(request, scenario):
        return False, False
    if _is_light_social_message(request) or _is_knowledge_overview_question(request):
        return True, False
    overview = _knowledge_source_summary(scenario.knowledge_source_id)
    if "no searchable files" in overview or "does not exist" in overview or "No knowledge source" in overview:
        return True, False
    topic_related = await _llm_topic_related(request, scenario)
    return (not topic_related), topic_related


def _latest_user_text(request: ChatRequest) -> str:
    return next(
        (message.content for message in reversed(request.messages) if message.role == "user"),
        "",
    )


def _is_knowledge_overview_question(request: ChatRequest) -> bool:
    text = _latest_user_text(request)
    return any(keyword in text for keyword in KNOWLEDGE_OVERVIEW_KEYWORDS)


def _is_light_social_message(request: ChatRequest) -> bool:
    text = _latest_user_text(request).strip().lower()
    normalized = text.strip("闂備線娼уΛ妤呭磻婵犲嫭顫曢柟鐑橆殕閺??,.闂?")
    if not normalized:
        return True
    if normalized in GREETING_PATTERNS:
        return True
    fuzzy_patterns = [pattern for pattern in GREETING_PATTERNS if pattern not in EXACT_GREETING_PATTERNS]
    return len(normalized) <= 12 and any(pattern in normalized for pattern in fuzzy_patterns)


def _is_appreciation_message(request: ChatRequest) -> bool:
    text = _latest_user_text(request).strip().lower()
    normalized = text.strip("闂備線娼уΛ妤呭磻婵犲嫭顫曢柟鐑橆殕閺??,.闂?")
    return 0 < len(normalized) <= 24 and any(pattern in normalized for pattern in APPRECIATION_PATTERNS)


def _knowledge_source_summary(source_id: str | None) -> str:
    if not source_id:
        return "No knowledge source is mounted."
    try:
        source = state.knowledge_store.get_source(source_id)
        files = state.knowledge_store.list_files(source_id)
    except HTTPException:
        return f"Mounted knowledge source `{source_id}` does not exist. Please choose another source."
    if not files:
        return (
            f"Mounted knowledge source is `{source.id}` ({source.name}), but it has no searchable files or chunks. "
            "Please upload materials first or switch to a source with files."
        )
    file_lines = [
        f"- {file.filename} ({file.chunk_count} chunks)"
        for file in files
    ]
    return "\n".join(
        [
            f"Mounted knowledge source: `{source.id}` ({source.name}).",
            "Indexed materials:",
            *file_lines,
            "You can ask questions around these materials.",
        ]
    )

def _strict_knowledge_message(request: ChatRequest, scenario: TeachingScenario) -> str:
    if _is_appreciation_message(request):
        return STRICT_APPRECIATION_MESSAGE
    if _is_light_social_message(request):
        return STRICT_GREETING_MESSAGE
    overview = _knowledge_source_summary(scenario.knowledge_source_id)
    if _is_knowledge_overview_question(request):
        return overview
    if "no searchable files" in overview or "does not exist" in overview or "No knowledge source" in overview:
        return overview
    return STRICT_KNOWLEDGE_MISS_MESSAGE


def _strict_knowledge_miss_response(request: ChatRequest, scenario: TeachingScenario) -> dict[str, Any]:
    message = _strict_knowledge_message(request, scenario)
    return {
        "id": f"chatcmpl-edugate-strict-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": scenario.model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": message,
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _strict_knowledge_miss_sse(request: ChatRequest, scenario: TeachingScenario):
    message = _strict_knowledge_message(request, scenario)
    chunk = {
        "id": f"chatcmpl-edugate-strict-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": scenario.model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": message},
                "finish_reason": None,
            }
        ],
    }
    done = {
        **chunk,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def _public_model(model: ModelCatalogItem) -> ModelCatalogPublicItem:
    return ModelCatalogPublicItem(
        id=model.id,
        name=model.name,
        provider=model.provider,
        description=model.description,
        source=model.source,
        base_url=model.base_url,
        provider_id=model.provider_id or _provider_catalog_id(model.provider, model.base_url),
        upstream_model_id=model.upstream_model_id or model.id,
        api_key_set=state.secret_store.has(model.credential_id),
    )


def _public_model_catalog() -> dict[str, ModelCatalogPublicItem]:
    return {
        model_id: _public_model(model)
        for model_id, model in state.runtime_config.data.model_catalog.items()
    }


def _provider_api_key(request: ModelProviderConnectionRequest) -> tuple[str, bool]:
    if request.api_key:
        return request.api_key, False

    normalized_base_url = request.base_url.rstrip("/").casefold()
    requested_provider_id = request.provider_id or _provider_catalog_id(request.provider, request.base_url)
    candidates: list[ModelCatalogItem] = []
    if request.credential_model_id:
        model = state.runtime_config.data.model_catalog.get(request.credential_model_id)
        if model is None:
            raise HTTPException(status_code=404, detail="找不到用于复用密钥的已有模型。")
        candidates.append(model)
    else:
        candidates.extend(state.runtime_config.data.model_catalog.values())

    for model in candidates:
        if model.source != "openai_compatible" or not model.base_url:
            continue
        if model.provider_id != requested_provider_id:
            continue
        if model.base_url.rstrip("/").casefold() != normalized_base_url:
            continue
        api_key = state.secret_store.get(model.credential_id)
        if api_key:
            return api_key, True
    raise HTTPException(status_code=400, detail="请填写 API Key，或先从同一接口地址的已有模型填入表单。")


async def _discover_provider_models(
    request: ModelProviderConnectionRequest,
) -> tuple[list[dict[str, str]], str, bool]:
    api_key, used_saved_key = _provider_api_key(request)
    try:
        models = await state.client.list_openai_models(base_url=request.base_url, api_key=api_key)
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300].strip() or error.response.reason_phrase
        raise HTTPException(
            status_code=502,
            detail=f"上游模型列表接口返回 HTTP {error.response.status_code}：{detail}",
        ) from error
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail="获取上游模型列表超时。") from error
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail=f"无法连接上游模型列表接口：{error!s}") from error
    return models, api_key, used_saved_key


def _direct_openai_model(model_id: str) -> ModelCatalogItem | None:
    model = state.runtime_config.data.model_catalog.get(model_id)
    if model and model.source == "openai_compatible":
        return model
    return None


def _validate_model_selection(model_id: str) -> None:
    if state.settings.deployment_mode != "standalone":
        return
    model = state.runtime_config.data.model_catalog.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_id}")
    if model.source == "openai_compatible" and (
        not model.base_url or not state.secret_store.has(model.credential_id)
    ):
        raise HTTPException(status_code=400, detail=f"Model is not fully configured: {model_id}")


async def _chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    direct_model = _direct_openai_model(str(payload.get("model", "")))
    if direct_model:
        api_key = state.secret_store.get(direct_model.credential_id)
        if not direct_model.base_url or not api_key:
            raise HTTPException(
                status_code=503,
                detail=f"Direct OpenAI-compatible model is missing base_url or api_key: {direct_model.id}",
            )
        async with state.model_semaphore:
            return await state.client.openai_chat_completion(
                base_url=direct_model.base_url,
                api_key=api_key,
                payload={
                    **payload,
                    "model": direct_model.upstream_model_id or direct_model.id,
                },
            )
    async with state.model_semaphore:
        return await state.client.chat_completion(payload)


async def _stream_chat_completion(payload: dict[str, Any]):
    direct_model = _direct_openai_model(str(payload.get("model", "")))
    if direct_model:
        api_key = state.secret_store.get(direct_model.credential_id)
        if not direct_model.base_url or not api_key:
            event = {
                "status_code": 503,
                "detail": f"Direct OpenAI-compatible model is missing base_url or api_key: {direct_model.id}",
            }
            yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        async with state.model_semaphore:
            async for chunk in state.client.stream_openai_chat_completion(
                base_url=direct_model.base_url,
                api_key=api_key,
                payload={
                    **payload,
                    "model": direct_model.upstream_model_id or direct_model.id,
                },
            ):
                yield chunk
        return
    async with state.model_semaphore:
        async for chunk in state.client.stream_chat_completion(payload):
            yield chunk


def _build_knowledge_context(
    request: ChatRequest,
    scenario: TeachingScenario,
    *,
    strict_topic_related: bool = False,
) -> str | None:
    if not scenario.knowledge_source_id:
        return None
    hits = _knowledge_hits(request, scenario)
    if not hits and not scenario.knowledge_strict:
        return None

    rules = [
        "The following content comes from the teacher-selected knowledge base. Prefer these snippets when answering.",
        "If the snippets are insufficient, say the materials do not provide enough evidence and guide the student with questions.",
        "Do not invent citations that are not in the knowledge base.",
    ]
    if scenario.knowledge_strict:
        rules.append("Strict mode: if the knowledge base is insufficient, do not complete the answer with outside knowledge.")
    snippets = [
        f"[{index}] Source: {hit.filename}; Chunk: {hit.text}"
        for index, hit in enumerate(hits, start=1)
    ]
    if not snippets:
        if strict_topic_related:
            snippets = [
                "The LLM topic gate judged this question related to the current knowledge-base topic, but keyword search found no exact chunk.",
                _knowledge_source_summary(scenario.knowledge_source_id),
                "Continue answering around the current knowledge-base topic and do not expand to unrelated topics.",
            ]
        else:
            snippets = ["No knowledge-base snippet matched the student question."]
    return "\n".join([*rules, "", "Knowledge-base snippets:", *snippets])

def _to_http_exception(error: httpx.HTTPStatusError) -> HTTPException:
    try:
        detail: Any = error.response.json()
    except ValueError:
        detail = error.response.text
    return HTTPException(status_code=error.response.status_code, detail=detail)


def _message_preview(messages: list[Any]) -> str | None:
    if not state.settings.log_message_preview:
        return None
    return latest_user_preview(messages)


def _latest_user_content(messages: list[Any]) -> str:
    return latest_user_preview(messages, limit=state.settings.classroom_record_max_content_chars)


def _chat_response_content(response: dict[str, Any] | None) -> str:
    if not response:
        return ""
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str):
        content = choice.get("text") if isinstance(choice.get("text"), str) else ""
    return content[: state.settings.classroom_record_max_content_chars]


def _record_classroom_turn(
    *,
    teacher_id: str | None,
    student: StudentIdentity,
    kind: str,
    input_content: str,
    output_content: str,
    status_code: int,
    latency_ms: int,
    queue_wait_ms: int | None = None,
    timed_out: bool | None = None,
) -> None:
    if not state.settings.classroom_recording_enabled or not teacher_id or not student.student_id:
        return
    try:
        state.business_db.record_classroom_turn(
            classroom_instance_id=state.classroom_access.classroom_id(),
            teacher_username=teacher_id,
            student_session_id=student.student_id,
            computer_name=student.computer_name,
            client_ip=student.client_ip,
            kind=kind,
            input_content=input_content,
            output_content=output_content,
            status_code=status_code,
            latency_ms=latency_ms,
            queue_wait_ms=queue_wait_ms,
            timed_out=timed_out,
        )
    except Exception as error:
        state.logger.warning("Failed to write classroom record: %s", error)


def _consume_sse_events(
    buffer: str,
) -> tuple[str, list[str], bool, list[dict[str, Any]]]:
    content: list[str] = []
    stream_done = False
    errors: list[dict[str, Any]] = []
    while True:
        boundary_index = buffer.find("\n\n")
        boundary_length = 2
        crlf_index = buffer.find("\r\n\r\n")
        if crlf_index >= 0 and (boundary_index < 0 or crlf_index < boundary_index):
            boundary_index = crlf_index
            boundary_length = 4
        if boundary_index < 0:
            break
        block = buffer[:boundary_index]
        buffer = buffer[boundary_index + boundary_length :]
        data = "\n".join(
            line.split(":", 1)[1].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        )
        event_name = next(
            (
                line.split(":", 1)[1].strip()
                for line in block.splitlines()
                if line.startswith("event:")
            ),
            "",
        )
        if not data:
            continue
        if data == "[DONE]":
            stream_done = True
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event_name == "error":
            errors.append(payload if isinstance(payload, dict) else {"detail": payload})
            continue
        choice = (payload.get("choices") or [{}])[0]
        value = (choice.get("delta") or {}).get("content")
        if not isinstance(value, str):
            value = (choice.get("message") or {}).get("content")
        if not isinstance(value, str):
            value = choice.get("text")
        if isinstance(value, str):
            content.append(value)
    return buffer, content, stream_done, errors


async def _trace_chat_result(
    *,
    route: str,
    request: ChatRequest,
    scenario: TeachingScenario,
    effective_scenario_id: str,
    teacher_id: str | None,
    response: dict[str, Any] | None,
    status_code: int,
    latency_ms: int,
    error: str | None = None,
) -> None:
    usage = response.get("usage") if response else None
    state.business_db.log_request(
        route=route,
        scenario_id=effective_scenario_id,
        teacher_id=teacher_id,
        model=scenario.model,
        knowledge_source_id=scenario.knowledge_source_id,
        user_message_preview=_message_preview(request.messages),
        status_code=status_code,
        latency_ms=latency_ms,
        usage=usage,
        error=error,
    )
    output = None
    if state.settings.log_message_preview and response:
        output = response.get("choices", [{}])[0].get("message", {}).get("content")
    await state.langfuse.trace_chat(
        name=route,
        input_text=_message_preview(request.messages),
        output_text=output,
        metadata={
            "route": route,
            "scenario_id": effective_scenario_id,
            "teacher_id": teacher_id,
            "model": scenario.model,
            "knowledge_source_id": scenario.knowledge_source_id,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error,
        },
        usage=usage,
    )


async def _stream_with_errors(payload: dict[str, Any]):
    try:
        async for chunk in _stream_with_heartbeat(_stream_chat_completion(payload)):
            yield chunk
    except httpx.HTTPStatusError as error:
        try:
            detail: Any = error.response.json()
        except ValueError:
            detail = error.response.text
        event = {"status_code": error.response.status_code, "detail": detail}
        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    except httpx.HTTPError as error:
        event = {
            "status_code": 502,
            "detail": f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        }
        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


async def _stream_with_heartbeat(source: AsyncIterator[bytes]):
    iterator = source.__aiter__()
    pending: asyncio.Task[bytes] | None = None
    try:
        pending = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait({pending}, timeout=state.settings.stream_heartbeat_seconds)
            if not done:
                yield b": edugate-keep-alive\n\n"
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                break
            yield chunk
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


async def _iterate_stream_bytes(source: AsyncIterator[bytes] | Iterable[bytes]):
    if hasattr(source, "__aiter__"):
        async for chunk in source:
            yield chunk
        return
    for chunk in source:
        yield chunk


async def _stream_with_completion_log(
    source: AsyncIterator[bytes] | Iterable[bytes],
    *,
    route: str,
    request: ChatRequest,
    scenario: TeachingScenario,
    effective_scenario_id: str,
    teacher_id: str | None,
    student: StudentIdentity | None,
):
    start = time.perf_counter()
    stream_chunks = 0
    stream_bytes = 0
    stream_done = False
    status_code = 200
    finish_reason = "ended_without_done"
    error_text: str | None = None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    event_buffer = ""
    assistant_parts: list[str] = []
    assistant_length = 0
    decoder_finalized = False

    def collect_events(decoded_text: str) -> None:
        nonlocal event_buffer, assistant_length, stream_done
        nonlocal status_code, finish_reason, error_text
        event_buffer += decoded_text
        event_buffer, extracted, observed_done, errors = _consume_sse_events(event_buffer)
        for content in extracted:
            remaining = state.settings.classroom_record_max_content_chars - assistant_length
            if remaining <= 0:
                break
            accepted = content[:remaining]
            assistant_parts.append(accepted)
            assistant_length += len(accepted)
        if observed_done:
            stream_done = True
            status_code = 200
            finish_reason = "done"
        for event in errors:
            try:
                status_code = int(event.get("status_code", 502))
            except (TypeError, ValueError):
                status_code = 502
            finish_reason = "upstream_error"
            detail = event.get("detail", event)
            error_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            error_text = error_text[:1000]

    def flush_pending_events() -> None:
        nonlocal decoder_finalized
        if decoder_finalized:
            return
        decoder_finalized = True
        collect_events(decoder.decode(b"", final=True) + "\n\n")

    def write_log() -> None:
        state.business_db.log_request(
            route=route,
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=status_code,
            latency_ms=now_ms(start),
            stream_done=stream_done,
            stream_chunks=stream_chunks,
            stream_bytes=stream_bytes,
            stream_duration_ms=now_ms(start),
            stream_finish_reason=finish_reason,
            error=error_text,
        )
        output = "".join(assistant_parts)
        if not output and error_text:
            output = error_text
        if student is not None:
            _record_classroom_turn(
                teacher_id=teacher_id,
                student=student,
                kind="chat",
                input_content=_latest_user_content(request.messages),
                output_content=output,
                status_code=status_code,
                latency_ms=now_ms(start),
            )

    try:
        async for chunk in _iterate_stream_bytes(source):
            stream_chunks += 1
            stream_bytes += len(chunk)
            collect_events(decoder.decode(chunk))
            yield chunk
    except asyncio.CancelledError:
        status_code = 499
        finish_reason = "client_disconnected"
        error_text = "Streaming response was cancelled before EduGate observed [DONE]."
        flush_pending_events()
        write_log()
        raise
    except Exception as error:
        status_code = 500
        finish_reason = "server_exception"
        error_text = f"{type(error).__name__}: {error!s}"
        flush_pending_events()
        write_log()
        raise
    else:
        flush_pending_events()
        if stream_done:
            finish_reason = "done"
            status_code = 200
        elif finish_reason == "ended_without_done":
            error_text = "Stream ended before EduGate observed [DONE]."
        write_log()
