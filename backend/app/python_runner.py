from __future__ import annotations

import asyncio
import codecs
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psutil

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


class PythonQueueFull(RuntimeError):
    pass


class PythonStudentBusy(RuntimeError):
    pass


class PythonQueueTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class PythonPoolResult:
    result: PythonRunResult
    job_id: str
    worker_id: int
    queue_wait_ms: int


@dataclass
class PythonJob:
    id: str
    code: str
    student_id: str
    submitted_at: float
    runner: Callable[..., PythonRunResult]
    runner_kwargs: dict[str, Any]
    future: asyncio.Future[PythonPoolResult]
    events: asyncio.Queue[dict[str, Any]]


class PythonExecutionPool:
    def __init__(
        self,
        *,
        max_workers: int,
        max_queue_size: int,
        queue_timeout_seconds: float,
    ) -> None:
        self.max_workers = min(max(1, max_workers), 8)
        self.max_queue_size = min(max(1, max_queue_size), 256)
        self.queue_timeout_seconds = min(max(0.1, queue_timeout_seconds), 300)
        self._queue: asyncio.Queue[PythonJob] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._active_students: set[str] = set()
        self._jobs: dict[str, PythonJob] = {}
        self._lock: asyncio.Lock | None = None
        self._running = 0

    async def start(self) -> None:
        if self._workers:
            return
        self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        self._lock = asyncio.Lock()
        self._workers = [
            asyncio.create_task(self._worker(index + 1), name=f"edugate-python-{index + 1}")
            for index in range(self.max_workers)
        ]

    async def stop(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        error = PythonRunnerUnavailable("Python execution pool is stopping")
        for job in list(self._jobs.values()):
            if not job.future.done():
                job.future.set_exception(error)
            self._emit(job, "error", {"job_id": job.id, "detail": str(error)})
        self._jobs.clear()
        self._active_students.clear()
        self._queue = None
        self._lock = None
        self._running = 0

    async def submit(
        self,
        code: str,
        *,
        student_id: str,
        runner: Callable[..., PythonRunResult] | None = None,
        **runner_kwargs: Any,
    ) -> PythonJob:
        if self._queue is None or self._lock is None or not self._workers:
            raise PythonRunnerUnavailable("Python execution pool is not running")
        runner = runner or run_python_code
        async with self._lock:
            if student_id in self._active_students:
                raise PythonStudentBusy("This student already has a queued or running Python task")
            if self._queue.full():
                raise PythonQueueFull("Python execution queue is full")
            loop = asyncio.get_running_loop()
            job = PythonJob(
                id=uuid.uuid4().hex,
                code=code,
                student_id=student_id,
                submitted_at=time.monotonic(),
                runner=runner,
                runner_kwargs=runner_kwargs,
                future=loop.create_future(),
                events=asyncio.Queue(),
            )
            self._active_students.add(student_id)
            self._jobs[job.id] = job
            self._queue.put_nowait(job)
            self._emit(
                job,
                "queued",
                {
                    "job_id": job.id,
                    "queue_position": self._queue.qsize(),
                    "workers": self.max_workers,
                },
            )
            return job

    async def execute(
        self,
        code: str,
        *,
        student_id: str,
        runner: Callable[..., PythonRunResult] | None = None,
        **runner_kwargs: Any,
    ) -> PythonPoolResult:
        job = await self.submit(
            code,
            student_id=student_id,
            runner=runner,
            **runner_kwargs,
        )
        return await job.future

    async def iter_events(self, job: PythonJob):
        while True:
            event = await job.events.get()
            yield event
            if event["event"] in {"done", "error"}:
                break

    def stats(self) -> dict[str, int]:
        return {
            "workers": self.max_workers,
            "running": self._running,
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "queue_capacity": self.max_queue_size,
            "active_students": len(self._active_students),
        }

    async def _worker(self, worker_id: int) -> None:
        assert self._queue is not None
        while True:
            job = await self._queue.get()
            queue_wait_ms = int((time.monotonic() - job.submitted_at) * 1000)
            started = False
            try:
                if queue_wait_ms > int(self.queue_timeout_seconds * 1000):
                    raise PythonQueueTimeout("Python task expired while waiting in the queue")
                self._running += 1
                started = True
                self._emit(
                    job,
                    "running",
                    {
                        "job_id": job.id,
                        "worker_id": worker_id,
                        "queue_wait_ms": queue_wait_ms,
                    },
                )
                loop = asyncio.get_running_loop()

                def on_output(stream: str, content: str) -> None:
                    loop.call_soon_threadsafe(
                        self._emit,
                        job,
                        stream,
                        {"job_id": job.id, "content": content},
                    )

                result = await asyncio.to_thread(
                    job.runner,
                    job.code,
                    on_output=on_output,
                    **job.runner_kwargs,
                )
                pooled = PythonPoolResult(
                    result=result,
                    job_id=job.id,
                    worker_id=worker_id,
                    queue_wait_ms=queue_wait_ms,
                )
                if not job.future.done():
                    job.future.set_result(pooled)
                self._emit(
                    job,
                    "done",
                    {
                        "job_id": job.id,
                        "worker_id": worker_id,
                        "queue_wait_ms": queue_wait_ms,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "duration_ms": result.duration_ms,
                    },
                )
            except asyncio.CancelledError:
                error = PythonRunnerUnavailable("Python execution pool is stopping")
                if not job.future.done():
                    job.future.set_exception(error)
                self._emit(job, "error", {"job_id": job.id, "detail": str(error)})
                raise
            except Exception as error:
                if not job.future.done():
                    job.future.set_exception(error)
                self._emit(
                    job,
                    "error",
                    {"job_id": job.id, "detail": str(error), "error_type": type(error).__name__},
                )
            finally:
                if started:
                    self._running -= 1
                self._active_students.discard(job.student_id)
                self._jobs.pop(job.id, None)
                self._queue.task_done()

    @staticmethod
    def _emit(job: PythonJob, event: str, data: dict[str, Any]) -> None:
        job.events.put_nowait({"event": event, "data": data})


def run_python_code(
    code: str,
    *,
    timeout_seconds: float = 3.0,
    output_limit: int = 12000,
    memory_limit_mb: int = 128,
    executable: str | None = None,
    on_output: Callable[[str, str], None] | None = None,
) -> PythonRunResult:
    start = time.perf_counter()
    python_executable = resolve_python_executable(executable)
    with tempfile.TemporaryDirectory(prefix="edugate-python-") as workdir:
        job_handle = None
        process: subprocess.Popen[bytes] | None = None
        try:
            env = _runner_environment(python_executable)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
                [python_executable, "-I", "-S", "-c", RUNNER_SCRIPT],
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
            memory_exceeded = threading.Event()
            memory_monitor = None
            if sys.platform == "darwin":  # pragma: no cover - exercised by the macOS bundle smoke test
                memory_monitor = threading.Thread(
                    target=_monitor_macos_memory,
                    args=(process, memory_limit_mb, memory_exceeded),
                    daemon=True,
                )
                memory_monitor.start()
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            output: dict[str, list[str]] = {"stdout": [], "stderr": []}
            output_lengths = {"stdout": 0, "stderr": 0}
            truncated: set[str] = set()

            def drain(stream_name: str, stream) -> None:
                decoder = io.IncrementalNewlineDecoder(
                    codecs.getincrementaldecoder("utf-8")("replace"),
                    translate=True,
                )
                read = getattr(stream, "read1", stream.read)
                while True:
                    chunk = read(1024)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    _append_runner_output(
                        stream_name,
                        text,
                        output,
                        output_lengths,
                        truncated,
                        output_limit,
                        on_output,
                    )
                tail = decoder.decode(b"", final=True)
                if tail:
                    _append_runner_output(
                        stream_name,
                        tail,
                        output,
                        output_lengths,
                        truncated,
                        output_limit,
                        on_output,
                    )

            readers = [
                threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()
            process.stdin.write(code.encode("utf-8"))
            process.stdin.close()
            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
            for reader in readers:
                reader.join(timeout=2)
            if memory_monitor is not None:
                memory_monitor.join(timeout=1)

            stdout = "".join(output["stdout"])
            stderr = "".join(output["stderr"])
            if "stdout" in truncated:
                stderr += "\n输出过长，已截断。"
            if "stderr" in truncated:
                stderr += "\n错误输出过长，已截断。"
            if timed_out:
                timeout_message = "程序运行超时，已终止。"
                stderr = (stderr + "\n" + timeout_message).strip()
                if on_output:
                    on_output("stderr", f"\n{timeout_message}\n")
            if memory_exceeded.is_set():  # pragma: no cover - exercised by the macOS bundle smoke test
                memory_message = "程序使用内存过多，已终止。"
                stderr = (stderr + "\n" + memory_message).strip()
                if on_output:
                    on_output("stderr", f"\n{memory_message}\n")
            return PythonRunResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=137 if memory_exceeded.is_set() else (124 if timed_out else int(process.returncode or 0)),
                timed_out=timed_out,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        finally:
            if process is not None and process.poll() is None:
                process.kill()
            if job_handle is not None:
                ctypes.windll.kernel32.CloseHandle(job_handle)


def _append_runner_output(
    stream_name: str,
    text: str,
    output: dict[str, list[str]],
    output_lengths: dict[str, int],
    truncated: set[str],
    output_limit: int,
    on_output: Callable[[str, str], None] | None,
) -> None:
    remaining = output_limit - output_lengths[stream_name]
    if remaining <= 0:
        truncated.add(stream_name)
        return
    accepted = text[:remaining]
    if accepted:
        output[stream_name].append(accepted)
        output_lengths[stream_name] += len(accepted)
        if on_output:
            try:
                on_output(stream_name, accepted)
            except Exception:
                pass
    if len(text) > remaining:
        truncated.add(stream_name)


def resolve_python_executable(configured: str | None = None) -> str:
    candidates: list[Path] = []
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute() and os.getenv("EDUGATE_APP_DIR"):
            configured_path = Path(os.environ["EDUGATE_APP_DIR"]) / configured_path
        candidates.append(configured_path)
    app_dir = Path(os.getenv("EDUGATE_APP_DIR") or Path(sys.executable).resolve().parent)
    if os.name == "nt":
        candidates.append(app_dir / "runtime" / "python" / "python.exe")
    else:
        candidates.append(app_dir / "runtime" / "python" / "bin" / "python")
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
        "or use a portable bundle that includes runtime/python."
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
    env.update(
        {
            "EDUGATE_STUDENT_RUNNER_MODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _unix_memory_limit(memory_limit_mb: int):
    if os.name == "nt" or sys.platform == "darwin":
        return None

    def apply_limit() -> None:
        import resource

        limit = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply_limit


def _monitor_macos_memory(
    process: subprocess.Popen[bytes],
    memory_limit_mb: int,
    exceeded: threading.Event,
) -> None:  # pragma: no cover - exercised by the macOS bundle smoke test
    limit = memory_limit_mb * 1024 * 1024
    try:
        monitored = psutil.Process(process.pid)
    except (psutil.Error, OSError):
        return
    while process.poll() is None:
        try:
            if monitored.memory_info().rss > limit:
                exceeded.set()
                process.kill()
                return
        except (psutil.Error, OSError, ProcessLookupError):
            return
        time.sleep(0.02)


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
