# Python Execution and Classroom Records Design

[中文](并发执行与课堂记录设计.md) · **English** · [Back to project home](../README.en.md)

This document defines the bounded Python execution pool and teacher-facing classroom records in EduGate Standalone. They share classroom-session identity and lifecycle rules, but never share student interpreter state.

## Goals

- Accept simultaneous submissions from a 64-student class while running only the number of child processes the teacher computer can support.
- Queue excess work within a hard bound instead of creating unlimited processes or consuming unbounded memory.
- Stream queued, running, stdout, stderr, completion, and error states to students.
- Let teachers review local activity by classroom, generated device label/IP, and activity type.
- Join silently using a stable browser device ID plus the request IP, without exposing one teacher's classroom content to another teacher.
- Allow recording to be disabled, expired automatically, and permanently deleted per class.

## Python execution pool

The queue is not a cache. It stores descriptions of tasks that have not started; it neither reuses results nor reuses interpreters. Each worker slot creates a fresh restricted Python subprocess with its own temporary directory.

| Setting | Default | Enforced range |
|---|---:|---:|
| `PYTHON_RUNNER_MAX_CONCURRENCY` | 4 | 1–8 |
| `PYTHON_RUNNER_MAX_QUEUE` | 64 | 1–256 |
| `PYTHON_RUNNER_QUEUE_TIMEOUT_SECONDS` | 30 | 1–300 seconds |
| `PYTHON_RUNNER_TIMEOUT_SECONDS` | 3 | 0.2–30 seconds |
| `PYTHON_RUNNER_MEMORY_MB` | 128 | 32–1024 MB/task |

One student session may have only one queued or running task. A full queue returns HTTP 429, duplicate occupancy returns HTTP 409, and a missing separate interpreter returns HTTP 503. `/run_python` preserves the complete JSON response for older clients. `/run_python/stream` emits `queued`, `running`, `stdout`, `stderr`, `done`, and `error` SSE events.

The pool improves throughput but does not turn the AST allowlist and Windows Job Object into a hardened hostile-code sandbox. It remains a short classroom-code facility, not a public online judge.

## Classroom record model

A classroom is scoped by the current classroom-link cycle and teacher account. Starting the service or rotating the link creates a new cycle. Rotation closes prior records and revokes old student sessions.

Each turn stores an internal student-session ID, a generated device label, request IP, activity type (`chat` or `python`), latest student question or code, complete AI response or Python output, timestamps, status, execution latency, Python queue wait, and timeout state. It does not store classroom tokens or student names and does not try to read a Windows computer name. On first use the browser creates a random local device ID and silently reuses it; the service derives a label such as `computer-A1B2C3` from its suffix. Legacy classroom-token clients fall back to `computer-<IP>` while retaining a private per-class session ID internally.

## Access, privacy, and lifecycle

- Regular teachers may list, read, and delete only their own classrooms.
- Administrators may review and delete all local teacher records.
- `CLASSROOM_RECORDING_ENABLED=false` stops new content collection without deleting existing data.
- Defaults are 30-day retention, 20,000 total turns, and 12,000 characters for each input and output field.
- Cleanup runs on the database writer timer every 300 seconds by default, and once at startup.
- Records live in `edugate.sqlite3`, are included in full backups, and cascade-delete with their classroom.
- Technical request logs and classroom content are separate controls. `LOG_MESSAGE_PREVIEW=false` does not disable classroom records, and disabling classroom records does not change technical logging.

## Asynchronous SQLite writes

Technical request logs and classroom turns enter a bounded in-memory queue. A dedicated SQLite writer combines them into transactions of up to 100 items, with a default maximum wait of 20 milliseconds. The default queue capacity is 4,096 items. A full queue drops only new log items and increments a warning counter, so log pressure cannot consume unbounded memory or hold up classroom responses.

Reading or deleting records, ending a class, creating a backup, and shutting down insert a write barrier and wait for all earlier items to commit. The system status endpoint reports queued, written, batched, dropped, and failed counts plus the last cleanup time. `DB_WRITE_QUEUE_SIZE`, `DB_WRITE_BATCH_SIZE`, `DB_WRITE_FLUSH_INTERVAL_MS`, and `DB_CLEANUP_INTERVAL_SECONDS` tune this behavior and require a restart.

## Teacher experience

The Records tab displays local-retention status, aggregate classroom metrics, a time-ordered classroom list, and a detail view filtered by generated device label/IP and AI/Python activity. Teachers can permanently delete a complete class record.

The first release intentionally excludes student accounts, names, persistent profiles, and automatic grading. Generated device labels and IPs exist only to locate a device in a teacher's class. Future export or analytics work should remain classroom-scoped rather than turning the standalone application into a school-level student-record system.
