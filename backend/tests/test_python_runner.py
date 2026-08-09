import os
import sys

import pytest

from app.python_runner import (
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
