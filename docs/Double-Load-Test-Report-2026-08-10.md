# Double-Load Test Report

[中文](双倍压力测试报告-2026-08-10.md) · **English** · [Back to project home](../README.en.md)

Test date: 2026-08-10

## Result

EduGate, using its default **16 model lanes**, completed twice the original classroom load. The final fast-model and 750 ms-model runs each passed 34/34 checks with no HTTP 429/500 responses, connection failures, SQLite lock errors, dropped logs, or failed writes.

Core throughput remained essentially unchanged under twice the demand. Higher P95 latency came from 128 students sharing a fixed 16-lane upstream pool. Sixty-four remains the recommended classroom size; 128 students are a stability margin test, not the same latency target.

The test did not set `MODEL_MAX_CONCURRENCY` through the environment. Runtime defaults, System settings, new-install configuration, and the local portable configuration all specify 16.

## Environment and workload

- Windows 10.0.19045, Intel Core i7-8700, 12 logical processors, 31.83 GB RAM.
- EduGate and the fake OpenAI-compatible model ran on loopback.
- Four Python workers with a 64-item waiting queue.
- Bounded batched SQLite writing with classroom and technical logging enabled.

| Phase | Original | Double load |
|---|---:|---:|
| Simultaneous joins | 64 | 128 |
| Health requests | 512 at 64 clients | 1,024 at 128 clients |
| Burst chats | 64 at 64 clients | 128 at 128 clients |
| Sustained chats | 256 at 64 clients | 512 at 128 clients |
| Streamed chats | 64 at 64 clients | 128 at 128 clients |
| Python jobs | 64 | 128 total, 64 in flight |

Python in-flight concurrency stayed at 64 so the test doubled completed volume without deliberately overflowing the default 64-item waiting queue.

## 75 ms model

| Phase | 64-student req/s | 128-student req/s | 64-student P95 | 128-student P95 | Result |
|---|---:|---:|---:|---:|---|
| Joins | 105.31 | 90.47 | 0.51 s | 1.27 s | 128/128 |
| Health | 88.05 | 77.35 | 1.83 s | 4.35 s | 1,024/1,024 |
| Burst chats | 55.24 | 55.80 | 1.04 s | 2.12 s | 128/128 |
| Sustained chats | 52.25 | 51.83 | 2.66 s | 6.51 s | 512/512 |
| Streamed chats | 48.71 | 47.01 | 1.17 s | 2.47 s | 128/128 |
| Python | 35.99 | 36.07 | 1.63 s | 3.27 s | 128/128 |

Burst throughput rose 1.0%, sustained throughput fell only 0.8%, and stream throughput fell 3.5%. P95 increased by roughly 2.0–2.4 times as twice as many clients queued behind nearly unchanged service capacity. Observed upstream concurrency rose from 8 to 13, below the default limit of 16. Peak memory increased by only 0.79 MB, from 57.96 to 58.75 MB.

## 750 ms model

| Phase | 64-student req/s | 128-student req/s | 64-student P95 | 128-student P95 | Result |
|---|---:|---:|---:|---:|---|
| Joins | 96.47 | 90.46 | 0.57 s | 1.27 s | 128/128 |
| Health | 88.99 | 78.68 | 1.84 s | 4.77 s | 1,024/1,024 |
| Burst chats | 18.32 | 18.97 | 3.30 s | 6.49 s | 128/128 |
| Sustained chats | 19.59 | 19.59 | 4.71 s | 7.00 s | 512/512 |
| Streamed chats | 16.91 | 18.60 | 3.53 s | 6.49 s | 128/128 |
| Python | 32.01 | 35.32 | 1.81 s | 3.09 s | 128/128 |

Burst throughput rose 3.5%, sustained throughput was identical, and stream throughput rose 10.0%. The slower model continuously used all 16 upstream lanes. Burst P95 approximately doubled, while sustained P95 rose 48.7% without nonlinear degradation. Peak memory increased by 5.73 MB, from 59.79 to 65.52 MB.

## Integrity

| Metric | 64 students | 128 students |
|---|---:|---:|
| Upstream model calls | 384 | 768 |
| Stream calls | 64 | 128 |
| SQLite enqueued/written | 839/839 | 1,671/1,671 |
| Final SQLite queue | 0 | 0 |
| Dropped/failed writes | 0/0 | 0/0 |

The fixed functional setup is not doubled, so the final write count is 1,671 rather than 1,678. All 128 student session IDs and tokens remained unique, and ending the classroom revoked every old link and session.

## Load-client keep-alive correction

The first two slow-model attempts produced a small number of client-side `httpx.ReadError` events during long phases while the server logged no application exception. The load client and Uvicorn had similar keep-alive expiry times, allowing the single-machine connection pool to race with server-side idle connection closure.

Setting the load client's `keepalive_expiry` to two seconds made it retire idle connections first. The final run completed every request. This changed only the local test harness, not EduGate product networking; browsers independently retire and recreate idle connections.

## Decision

The default of 16 remains appropriate. It preserves throughput and data integrity at twice the target load without increasing the universal upstream burst to 32. A 128-student class will wait longer because it creates more waves through the same 16 lanes. Python remains sized for a 64-student classroom even though 128 jobs completed successfully in this volume test.

Raw JSON and isolated databases remain locally under `artifacts/double-load-2026-08-10` and are intentionally not committed.
