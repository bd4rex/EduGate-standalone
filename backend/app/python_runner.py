from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    import ctypes
    from ctypes import wintypes


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


class PythonRunnerUnavailable(RuntimeError):
    pass


def run_python_code(
    code: str,
    *,
    timeout_seconds: float = 3.0,
    output_limit: int = 12000,
    memory_limit_mb: int = 128,
    executable: str | None = None,
) -> PythonRunResult:
    start = time.perf_counter()
    python_executable = resolve_python_executable(executable)
    with tempfile.TemporaryDirectory(prefix="edugate-python-") as workdir:
        job_handle = None
        try:
            env = _runner_environment(python_executable)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                [python_executable, "-I", "-S", "-c", RUNNER_SCRIPT],
                text=True,
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=_unix_memory_limit(memory_limit_mb),
                creationflags=creationflags,
            )
            if os.name == "nt":
                job_handle = _assign_windows_job(process, memory_limit_mb)
            stdout_full, stderr_full = process.communicate(code, timeout=timeout_seconds)
            stdout = stdout_full[:output_limit]
            stderr = stderr_full[:output_limit]
            if len(stdout_full) > output_limit:
                stderr += "\n输出过长，已截断。"
            if len(stderr_full) > output_limit:
                stderr += "\n错误输出过长，已截断。"
            return PythonRunResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                timed_out=False,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except subprocess.TimeoutExpired:
            process.kill()
            stdout_full, stderr_full = process.communicate()
            return PythonRunResult(
                stdout=(stdout_full or "")[:output_limit],
                stderr=((stderr_full or "")[:output_limit] + "\n程序运行超时，已终止。").strip(),
                exit_code=124,
                timed_out=True,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        finally:
            if job_handle is not None:
                ctypes.windll.kernel32.CloseHandle(job_handle)


def resolve_python_executable(configured: str | None = None) -> str:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    data_dir = Path(
        os.getenv("EDUGATE_DATA_DIR")
        or Path(os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or Path.home()) / "EduGate"
    )
    if os.name == "nt":
        candidates.append(data_dir / "venv" / "Scripts" / "python.exe")
    else:
        candidates.append(data_dir / "venv" / "bin" / "python")
    if not getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise PythonRunnerUnavailable(
        "Python runner executable was not found. Configure PYTHON_RUNNER_EXECUTABLE "
        "or run the standalone dependency installer first."
    )


def _runner_environment(python_executable: str | None = None) -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    path_entries = []
    if python_executable:
        path_entries.append(str(Path(python_executable).resolve().parent))
    if not getattr(sys, "frozen", False):
        path_entries.append(str(Path(sys.base_prefix).resolve()))
    system_root = env.get("SYSTEMROOT") or env.get("WINDIR")
    if system_root:
        path_entries.extend([str(Path(system_root) / "System32"), system_root])
    if path_entries:
        env["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    return env


def _unix_memory_limit(memory_limit_mb: int):
    if os.name == "nt":
        return None

    def apply_limit() -> None:
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply_limit


if os.name == "nt":
    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]


    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    def _assign_windows_job(process: subprocess.Popen[str], memory_limit_mb: int):
        job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError()
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000
        info.ProcessMemoryLimit = memory_limit_mb * 1024 * 1024
        if not ctypes.windll.kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            ctypes.windll.kernel32.CloseHandle(job)
            raise ctypes.WinError()
        if not ctypes.windll.kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            ctypes.windll.kernel32.CloseHandle(job)
            process.kill()
            raise ctypes.WinError()
        return job
else:
    def _assign_windows_job(process: subprocess.Popen[str], memory_limit_mb: int):
        return None
