from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core import (
    _build_chat_payload,
    _chat_completion,
    _chat_response_content,
    _latest_user_content,
    _message_preview,
    _record_classroom_turn,
    _resolve_chat_context,
    _stream_with_completion_log,
    _stream_with_errors,
    _strict_knowledge_miss_response,
    _strict_knowledge_miss_sse,
    _strict_miss_decision,
    _to_http_exception,
    _trace_chat_result,
    business_db,
    now_ms,
    require_classroom_access,
    require_platform_key,
    runtime_config,
    settings,
)
from app.schemas import ChatRequest, ClientMessage, V1ChatCompletionRequest
from app.security import StudentIdentity

router = APIRouter()


@router.post("/chat")
async def chat(
    request: ChatRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> dict[str, Any]:
    effective_scenario_id, scenario, _ = _resolve_chat_context(request)
    teacher_id = settings.admin_username
    start = time.perf_counter()
    try:
        should_block, strict_topic_related = await _strict_miss_decision(request, scenario)
        if should_block:
            response = _strict_knowledge_miss_response(request, scenario)
        else:
            response = await _chat_completion(
                _build_chat_payload(
                    request,
                    scenario=scenario,
                    strict_topic_related=strict_topic_related,
                )
            )
        await _trace_chat_result(
            route="/chat",
            request=request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            response=response,
            status_code=200,
            latency_ms=now_ms(start),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=_chat_response_content(response),
            status_code=200,
            latency_ms=now_ms(start),
        )
        return response
    except httpx.HTTPStatusError as error:
        latency = now_ms(start)
        business_db.log_request(
            route="/chat",
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=error.response.status_code,
            latency_ms=latency,
            error=str(error),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=str(error),
            status_code=error.response.status_code,
            latency_ms=latency,
        )
        raise _to_http_exception(error) from error
    except httpx.HTTPError as error:
        latency = now_ms(start)
        business_db.log_request(
            route="/chat",
            scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            model=scenario.model,
            knowledge_source_id=scenario.knowledge_source_id,
            user_message_preview=_message_preview(request.messages),
            status_code=502,
            latency_ms=latency,
            error=str(error),
        )
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="chat",
            input_content=_latest_user_content(request.messages),
            output_content=str(error),
            status_code=502,
            latency_ms=latency,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        ) from error


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> StreamingResponse:
    effective_scenario_id, scenario, _ = _resolve_chat_context(request)
    teacher_id = settings.admin_username
    should_block, strict_topic_related = await _strict_miss_decision(request, scenario)
    if should_block:
        return StreamingResponse(
            _stream_with_completion_log(
                _strict_knowledge_miss_sse(request, scenario),
                route="/chat/stream",
                request=request,
                scenario=scenario,
                effective_scenario_id=effective_scenario_id,
                teacher_id=teacher_id,
                student=student,
            ),
            media_type="text/event-stream",
        )
    payload = _build_chat_payload(
        request,
        scenario=scenario,
        stream=True,
        strict_topic_related=strict_topic_related,
    )
    return StreamingResponse(
        _stream_with_completion_log(
            _stream_with_errors(payload),
            route="/chat/stream",
            request=request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            student=student,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/v1/chat/completions",
    dependencies=[Depends(require_platform_key)],
    response_model=None,
)
async def v1_chat_completions(request: V1ChatCompletionRequest):
    scenario_id = _v1_scenario_id(request)
    conversation = [
        ClientMessage(role=message.role, content=message.content)
        for message in request.messages
        if message.role != "system"
    ]
    if not conversation:
        raise HTTPException(status_code=400, detail="At least one user or assistant message is required")
    chat_request = ChatRequest(
        messages=conversation,
        scenario_id=scenario_id,
    )
    effective_scenario_id, scenario, _ = _resolve_chat_context(chat_request)
    teacher_id = settings.admin_username
    public_model_id = _v1_public_model_id(effective_scenario_id)
    should_block, strict_topic_related = await _strict_miss_decision(chat_request, scenario)

    if request.stream:
        if should_block:
            source = _strict_knowledge_miss_sse(chat_request, scenario)
        else:
            payload = _v1_payload(
                request,
                chat_request,
                scenario=scenario,
                stream=True,
                strict_topic_related=strict_topic_related,
            )
            source = _stream_with_errors(payload)
        return StreamingResponse(
            _stream_with_completion_log(
                source,
                route="/v1/chat/completions",
                request=chat_request,
                scenario=scenario,
                effective_scenario_id=effective_scenario_id,
                teacher_id=teacher_id,
                student=None,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-EduGate-Model": public_model_id,
            },
        )

    start = time.perf_counter()
    try:
        if should_block:
            response = _strict_knowledge_miss_response(chat_request, scenario)
        else:
            response = await _chat_completion(
                _v1_payload(
                    request,
                    chat_request,
                    scenario=scenario,
                    strict_topic_related=strict_topic_related,
                )
            )
        response = _normalize_v1_response(response, public_model_id)
        await _trace_chat_result(
            route="/v1/chat/completions",
            request=chat_request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            response=response,
            status_code=200,
            latency_ms=now_ms(start),
        )
        return response
    except httpx.HTTPStatusError as error:
        await _trace_chat_result(
            route="/v1/chat/completions",
            request=chat_request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            response=None,
            status_code=error.response.status_code,
            latency_ms=now_ms(start),
            error=str(error),
        )
        raise _to_http_exception(error) from error
    except httpx.HTTPError as error:
        await _trace_chat_result(
            route="/v1/chat/completions",
            request=chat_request,
            scenario=scenario,
            effective_scenario_id=effective_scenario_id,
            teacher_id=teacher_id,
            response=None,
            status_code=502,
            latency_ms=now_ms(start),
            error=str(error),
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream provider connection failed: {type(error).__name__}: {error!s}",
        ) from error


@router.get(
    "/v1/models",
    dependencies=[Depends(require_platform_key)],
)
async def v1_models() -> dict[str, Any]:
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": _v1_public_model_id(scenario_id),
                "object": "model",
                "created": created,
                "owned_by": "edugate",
                "scenario_id": scenario_id,
                "upstream_model": scenario.model,
            }
            for scenario_id, scenario in sorted(runtime_config.data.scenarios.items())
        ],
    }


def _v1_public_model_id(scenario_id: str) -> str:
    return "edugate" if scenario_id == "default" else f"edugate:{scenario_id}"


def _v1_scenario_id(request: V1ChatCompletionRequest) -> str:
    requested_model = (request.model or "").strip()
    scenario_id = request.scenario_id
    if requested_model.startswith("edugate:"):
        scenario_id = requested_model.split(":", 1)[1].strip()
        if not scenario_id:
            raise HTTPException(status_code=404, detail="Unknown model: empty EduGate scenario")
    scenario = runtime_config.get_scenario(scenario_id)
    accepted_models = {"", "edugate", _v1_public_model_id(scenario_id), scenario.model}
    if requested_model not in accepted_models:
        raise HTTPException(status_code=404, detail=f"Unknown model: {requested_model}")
    return scenario_id


def _v1_payload(
    request: V1ChatCompletionRequest,
    chat_request: ChatRequest,
    *,
    scenario: Any,
    stream: bool = False,
    strict_topic_related: bool = False,
) -> dict[str, Any]:
    payload = _build_chat_payload(
        chat_request,
        scenario=scenario,
        stream=stream,
        strict_topic_related=strict_topic_related,
    )
    system_messages = [
        message.model_dump()
        for message in request.messages
        if message.role == "system"
    ]
    insert_at = 0
    while insert_at < len(payload["messages"]) and payload["messages"][insert_at].get("role") == "system":
        insert_at += 1
    payload["messages"][insert_at:insert_at] = system_messages
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    return payload


def _normalize_v1_response(response: dict[str, Any], model_id: str) -> dict[str, Any]:
    normalized = dict(response)
    normalized.setdefault("id", f"chatcmpl-edugate-{int(time.time() * 1000)}")
    normalized.setdefault("object", "chat.completion")
    normalized.setdefault("created", int(time.time()))
    normalized["model"] = model_id
    normalized.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    return normalized
