from app.python_runner import run_python_code


def test_python_runner_executes_basic_code() -> None:
    result = run_python_code("for i in range(3):\n    print(i * i)")
    assert result.exit_code == 0
    assert result.stdout == "0\n1\n4\n"


def test_python_runner_timeout() -> None:
    result = run_python_code("while True:\n    pass", timeout_seconds=0.2)
    assert result.exit_code == 124
    assert result.timed_out is True
    assert "程序运行超时" in result.stderr
