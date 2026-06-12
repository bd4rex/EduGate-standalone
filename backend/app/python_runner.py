import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass


FORBIDDEN_NODES = (
    "Import",
    "ImportFrom",
    "Global",
    "Nonlocal",
)

FORBIDDEN_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "dir",
    "eval",
    "exec",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "vars",
}

SAFE_BUILTINS = {
    "abs",
    "all",
    "any",
    "bool",
    "chr",
    "dict",
    "enumerate",
    "Exception",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "pow",
    "print",
    "range",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "zip",
}

RUNNER_SCRIPT = f"""
import ast
import sys
import traceback

FORBIDDEN_NODES = {FORBIDDEN_NODES!r}
FORBIDDEN_NAMES = {sorted(FORBIDDEN_NAMES)!r}
SAFE_BUILTINS = {sorted(SAFE_BUILTINS)!r}

def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)

code = sys.stdin.read()

try:
    tree = ast.parse(code, filename="<student-code>", mode="exec")
    for node in ast.walk(tree):
        node_name = type(node).__name__
        if node_name in FORBIDDEN_NODES:
            fail(f"不允许使用 {{node_name}}。课堂演示接口只支持基础 Python 语法。")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            fail(f"不允许使用 {{node.id}}。")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            fail("不允许访问双下划线属性。")
    safe_builtins = {{
        name: getattr(__builtins__, name)
        for name in SAFE_BUILTINS
        if hasattr(__builtins__, name)
    }}
    namespace = {{"__builtins__": safe_builtins}}
    exec(compile(tree, "<student-code>", "exec"), namespace, namespace)
except SystemExit:
    raise
except BaseException as exc:
    traceback.print_exception(type(exc), exc, None)
    raise SystemExit(1)
"""


@dataclass(frozen=True)
class PythonRunResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int


def run_python_code(code: str, *, timeout_seconds: float = 3.0, output_limit: int = 12000) -> PythonRunResult:
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="edugate-python-") as workdir:
        try:
            env = os.environ.copy()
            env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", RUNNER_SCRIPT],
                input=code,
                text=True,
                cwd=workdir,
                env=env,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            stdout = completed.stdout[:output_limit]
            stderr = completed.stderr[:output_limit]
            if len(completed.stdout) > output_limit:
                stderr += "\n输出过长，已截断。"
            if len(completed.stderr) > output_limit:
                stderr += "\n错误输出过长，已截断。"
            return PythonRunResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=completed.returncode,
                timed_out=False,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except subprocess.TimeoutExpired as error:
            stdout = (error.stdout or "")[:output_limit]
            stderr = (error.stderr or "")[:output_limit]
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return PythonRunResult(
                stdout=stdout,
                stderr=(stderr + "\n程序运行超时，已终止。").strip(),
                exit_code=124,
                timed_out=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
