import asyncio
import os
import sys
import threading
import time

import pytest

from app.python_runner import (
    PythonExecutionPool,
    PythonQueueFull,
    PythonRunResult,
    PythonStudentBusy,
    PythonRunnerUnavailable,
    _runner_environment,
    resolve_python_executable,
    run_python_code,
)


def test_python_runner_executes_basic_code() -> None:
    result = run_python_code("for i in range(3):\n    print(i * i)")
    assert result.exit_code == 0
    assert result.stdout == "0\n1\n4\n"


def test_python_runner_timeout() -> None:
    result = run_python_code("while True:\n    pass", timeout_seconds=0.2)
    assert result.exit_code == 124
    assert result.timed_out is True
    assert "程序运行超时" in result.stderr


def test_python_runner_emits_output_before_process_completion() -> None:
    first_output = threading.Event()
    holder = {}

    def on_output(stream: str, content: str) -> None:
        if stream == "stdout" and "started" in content:
            first_output.set()

    def execute() -> None:
        holder["result"] = run_python_code(
            "print('started', flush=True)\nwhile True:\n    pass",
            timeout_seconds=0.6,
            on_output=on_output,
        )

    thread = threading.Thread(target=execute)
    thread.start()
    assert first_output.wait(timeout=0.4)
    assert thread.is_alive()
    thread.join(timeout=2)
    assert holder["result"].timed_out is True


def test_python_runner_does_not_inherit_application_secrets(monkeypatch) -> None:
    monkeypatch.setenv("UPSTREAM_API_KEY", "must-not-enter-student-process")
    monkeypatch.setenv("LITELLM_API_KEY", "must-not-enter-student-process")

    env = _runner_environment()

    assert "UPSTREAM_API_KEY" not in env
    assert "LITELLM_API_KEY" not in env
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_configured_python_executable_is_used(tmp_path) -> None:
    executable = tmp_path / ("python.exe" if os.name == "nt" else "python")
    executable.write_bytes(b"")

    assert resolve_python_executable(str(executable)) == str(executable.resolve())


def test_frozen_bundle_requires_a_separate_python_executable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EDUGATE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    with pytest.raises(PythonRunnerUnavailable, match="PYTHON_RUNNER_EXECUTABLE"):
        resolve_python_executable()


def test_python_pool_runs_multiple_isolated_jobs_with_bounded_concurrency() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_runner(code: str, *, on_output=None, **kwargs) -> PythonRunResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        if on_output:
            on_output("stdout", code)
        time.sleep(0.05)
        with lock:
            active -= 1
        return PythonRunResult(code, "", 0, False, 50)

    async def scenario() -> None:
        pool = PythonExecutionPool(max_workers=3, max_queue_size=8, queue_timeout_seconds=2)
        await pool.start()
        try:
            jobs = [
                await pool.submit(str(index), student_id=f"student-{index}", runner=fake_runner)
                for index in range(6)
            ]
            results = await asyncio.gather(*(job.future for job in jobs))
            assert {result.result.stdout for result in results} == {str(index) for index in range(6)}
            assert peak == 3
        finally:
            await pool.stop()

    asyncio.run(scenario())


def test_python_pool_rejects_queue_overflow_and_duplicate_student_jobs() -> None:
    release = threading.Event()

    def blocking_runner(code: str, *, on_output=None, **kwargs) -> PythonRunResult:
        release.wait(timeout=2)
        return PythonRunResult(code, "", 0, False, 1)

    async def scenario() -> None:
        pool = PythonExecutionPool(max_workers=1, max_queue_size=1, queue_timeout_seconds=2)
        await pool.start()
        try:
            first = await pool.submit("first", student_id="student-1", runner=blocking_runner)
            while pool.stats()["running"] != 1:
                await asyncio.sleep(0.005)
            with pytest.raises(PythonStudentBusy):
                await pool.submit("duplicate", student_id="student-1", runner=blocking_runner)
            second = await pool.submit("second", student_id="student-2", runner=blocking_runner)
            with pytest.raises(PythonQueueFull):
                await pool.submit("overflow", student_id="student-3", runner=blocking_runner)
            release.set()
            await asyncio.gather(first.future, second.future)
        finally:
            release.set()
            await pool.stop()

    asyncio.run(scenario())


def test_python_pool_streams_queue_state_output_and_completion() -> None:
    def streaming_runner(code: str, *, on_output=None, **kwargs) -> PythonRunResult:
        assert on_output is not None
        on_output("stdout", "first\n")
        on_output("stderr", "warning\n")
        return PythonRunResult("first\n", "warning\n", 0, False, 1)

    async def scenario() -> None:
        pool = PythonExecutionPool(max_workers=2, max_queue_size=4, queue_timeout_seconds=2)
        await pool.start()
        try:
            job = await pool.submit("print(1)", student_id="student-stream", runner=streaming_runner)
            events = [event async for event in pool.iter_events(job)]
            assert [event["event"] for event in events] == [
                "queued",
                "running",
                "stdout",
                "stderr",
                "done",
            ]
            assert events[-1]["data"]["exit_code"] == 0
        finally:
            await pool.stop()

    asyncio.run(scenario())
