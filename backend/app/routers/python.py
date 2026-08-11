from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core import (
    _record_classroom_turn,
    business_db,
    python_pool,
    python_record_tasks,
    require_classroom_access,
    run_python_code,
    settings,
)
from app.python_runner import (
    PythonJob,
    PythonQueueFull,
    PythonQueueTimeout,
    PythonRunnerUnavailable,
    PythonStudentBusy,
)
from app.schemas import PythonRunRequest, PythonRunResponse
from app.security import StudentIdentity

router = APIRouter()


def _python_http_error(error: Exception) -> HTTPException:
    if isinstance(error, PythonStudentBusy):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (PythonQueueFull, PythonQueueTimeout)):
        return HTTPException(status_code=429, detail=str(error))
    return HTTPException(status_code=503, detail=str(error))


async def _python_sse_events(job: PythonJob):
    iterator = python_pool.iter_events(job).__aiter__()
    pending: asyncio.Task[dict[str, Any]] | None = None
    try:
        pending = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait({pending}, timeout=settings.stream_heartbeat_seconds)
            if not done:
                yield b": edugate-python-keep-alive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                break
            event_name = item["event"]
            payload = json.dumps(item["data"], ensure_ascii=False)
            yield f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")
            if event_name in {"done", "error"}:
                break
            pending = asyncio.create_task(iterator.__anext__())
    finally:
        if pending and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


async def _submit_python_job(request: PythonRunRequest, student: StudentIdentity) -> PythonJob:
    if not settings.python_runner_enabled:
        raise HTTPException(status_code=503, detail="Python runner is disabled")
    try:
        job = await python_pool.submit(
            request.code,
            student_id=student.student_id,
            runner=run_python_code,
            timeout_seconds=settings.python_runner_timeout_seconds,
            memory_limit_mb=settings.python_runner_memory_mb,
            executable=settings.python_runner_executable,
        )
        _track_python_record(job, request=request, student=student)
        return job
    except (PythonRunnerUnavailable, PythonStudentBusy, PythonQueueFull, PythonQueueTimeout) as error:
        raise _python_http_error(error) from error


def _track_python_record(job: PythonJob, *, request: PythonRunRequest, student: StudentIdentity) -> None:
    if not settings.classroom_recording_enabled:
        return
    teacher_id = settings.admin_username
    teacher = business_db.get_teacher(teacher_id)
    if not teacher or not teacher.get("is_active"):
        return

    async def monitor() -> None:
        try:
            pooled = await asyncio.shield(job.future)
        except Exception as error:
            if isinstance(error, PythonRunnerUnavailable):
                status_code = 503
            elif isinstance(error, (PythonQueueFull, PythonQueueTimeout, PythonStudentBusy)):
                status_code = 429
            else:
                status_code = 500
            _record_classroom_turn(
                teacher_id=teacher_id,
                student=student,
                kind="python",
                input_content=request.code,
                output_content=str(error),
                status_code=status_code,
                latency_ms=int((time.monotonic() - job.submitted_at) * 1000),
            )
            return
        result = pooled.result
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(result.stderr)
        _record_classroom_turn(
            teacher_id=teacher_id,
            student=student,
            kind="python",
            input_content=request.code,
            output_content="\n".join(output_parts),
            status_code=200,
            latency_ms=result.duration_ms + pooled.queue_wait_ms,
            queue_wait_ms=pooled.queue_wait_ms,
            timed_out=result.timed_out,
        )

    task = asyncio.create_task(monitor(), name=f"record-python-{job.id}")
    python_record_tasks.add(task)
    task.add_done_callback(python_record_tasks.discard)


@router.post("/run_python", response_model=PythonRunResponse)
async def run_python(
    request: PythonRunRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> PythonRunResponse:
    job = await _submit_python_job(request, student)
    try:
        pooled = await job.future
    except (PythonRunnerUnavailable, PythonStudentBusy, PythonQueueFull, PythonQueueTimeout) as error:
        raise _python_http_error(error) from error
    result = pooled.result
    return PythonRunResponse(
        job_id=pooled.job_id,
        worker_id=pooled.worker_id,
        queue_wait_ms=pooled.queue_wait_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
    )


@router.post("/run_python/stream")
async def run_python_stream(
    request: PythonRunRequest,
    student: StudentIdentity = Depends(require_classroom_access),
) -> StreamingResponse:
    job = await _submit_python_job(request, student)
    return StreamingResponse(
        _python_sse_events(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
