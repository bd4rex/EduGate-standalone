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
    settings,
)
from app.schemas import ChatRequest, V1ChatCompletionRequest
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
    )


@router.post(
    "/v1/chat/completions",
    dependencies=[Depends(require_platform_key)],
    response_model=None,
)
async def v1_chat_completions(request: V1ChatCompletionRequest):
    chat_request = ChatRequest(
        messages=request.messages,
        scenario_id=request.scenario_id,
    )
    if request.stream:
        _, scenario, _ = _resolve_chat_context(chat_request)
        should_block, strict_topic_related = await _strict_miss_decision(chat_request, scenario)
        if should_block:
            return StreamingResponse(_strict_knowledge_miss_sse(chat_request, scenario), media_type="text/event-stream")
        payload = _build_chat_payload(
            chat_request,
            scenario=scenario,
            stream=True,
            strict_topic_related=strict_topic_related,
        )
        return StreamingResponse(_stream_with_errors(payload), media_type="text/event-stream")
    return await chat(chat_request, student=StudentIdentity(student_id="", computer_name="", client_ip=""))
