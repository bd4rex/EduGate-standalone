# Model Concurrency Benchmark

[中文](模型并发基准报告-2026-08-10.md) · **English** · [Back to project home](../README.en.md)

Test date: 2026-08-10

## Decision

EduGate Standalone now defaults `MODEL_MAX_CONCURRENCY` to **16**, while Advanced Settings continues to allow values from 1 to 32.

- Four lanes leave a 64-student class waiting too long.
- Eight lanes are the knee for very fast models, but slower responses still queue noticeably.
- Sixteen lanes cover both fast and slower models and split a 64-student burst into at most four upstream waves.
- Thirty-two lanes remain a performance preset. Against 16 lanes, they doubled permitted upstream pressure while improving sustained throughput by 47% and sustained P95 by only 9.3%; throughput per permitted lane fell by about 26.5%.

Sixteen is therefore the best default balance for this teacher PC, a 64-student classroom, and compatibility across upstream vendors. Use 32 only after confirming the provider quota; use 8 when the quota is unknown.

## Environment and workload

- Windows 10.0.19045, Intel Core i7-8700, 12 logical processors, 31.83 GB RAM.
- EduGate and the fake OpenAI-compatible model ran on loopback.
- Four Python workers, bounded batched SQLite writes, classroom and technical logging enabled.
- No real API key or billable model traffic.
- Two complete-response delays: 75 ms and 750 ms.

Each fresh instance ran 34 functional checks, 64 simultaneous joins, 512 health requests, 64 burst chats, 256 sustained chats at 64 client concurrency, 64 streamed chats, and 64 Python jobs. Each model phase made 384 upstream calls and persisted 839 asynchronous SQLite items.

## 75 ms model

| Limit | Observed peak | Burst req/s | Burst P95 | Sustained req/s | Sustained P95 | Stream req/s | Stream P95 | Peak memory |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 4 | 35.12 | 1.70 s | 36.03 | 4.43 s | 36.00 | 1.56 s | 57.88 MB |
| 8 | 8 | 58.17 | 0.99 s | 55.88 | 2.55 s | 52.73 | 1.08 s | 56.79 MB |
| 12 | 9 | 60.22 | 0.95 s | 56.94 | 2.29 s | 60.42 | 0.95 s | 58.02 MB |
| 16 | 8 | 55.24 | 1.04 s | 52.25 | 2.66 s | 48.71 | 1.17 s | 57.96 MB |
| 24 | 8 | 61.01 | 0.96 s | 55.83 | 2.55 s | 56.49 | 1.02 s | 58.48 MB |
| 32 | 9 | 61.13 | 0.93 s | 56.82 | 2.92 s | 60.66 | 0.95 s | 58.17 MB |

The fast profile reaches a local plateau around 56–61 req/s after eight lanes. Raising the setting from 8 to 32 improved sustained throughput by only 1.7%, and observed upstream concurrency never exceeded 9. A same-machine direct baseline reached 88.87 req/s with a 0.63-second P95, but bypassed EduGate identity, policy, knowledge, throttling, and recording work.

## 750 ms model

| Limit | Observed peak | Burst req/s | Burst P95 | Sustained req/s | Sustained P50 | Sustained P95 | Stream req/s | Stream P95 | Peak memory |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 4 | 5.03 | 12.60 s | 5.11 | 12.37 s | 12.60 s | 4.94 | 12.81 s | 59.05 MB |
| 8 | 8 | 9.79 | 6.42 s | 10.01 | 6.25 s | 6.54 s | 9.49 | 6.57 s | 58.97 MB |
| 16 | 16 | 18.32 | 3.30 s | 19.59 | 3.11 s | 4.71 s | 16.91 | 3.53 s | 59.79 MB |
| 32 | 32 | 29.11 | 2.08 s | 28.80 | 1.65 s | 4.27 s | 22.88 | 2.61 s | 61.17 MB |

Against eight lanes, 16 increased burst throughput by 87.1%, sustained throughput by 95.7%, and stream throughput by 78.2%, while reducing their P95 values by 48.7%, 28.1%, and 46.2%. Thirty-two remained faster, but its sustained P95 improved by only another 9.3% while the configured concurrency doubled.

## Correctness and provider limits

Every formal run passed 34/34 checks. All requests succeeded; every run wrote 839/839 database items with zero drops, zero failures, an empty final queue, and no SQLite lock errors. Observed upstream concurrency never exceeded the configured limit.

As of the test date, the [official DeepSeek rate-limit documentation](https://api-docs.deepseek.com/quick_start/rate_limit) lists account concurrency limits of 500 for `deepseek-v4-pro` and 2,500 for `deepseek-v4-flash`; requests beyond the account limit receive HTTP 429. The new default of 16 is well below those values. Other OpenAI-compatible providers can have very different limits.

| Situation | Recommended value |
|---|---:|
| Unknown provider quota | 8 |
| Normal 64-student class | **16** |
| Confirmed quota, performance first | 32 |
| Provider explicitly allows N | Start with `min(N, 16)` |

The setting is under System → Advanced Settings → Model maximum concurrency and requires a restart. Concurrency does not change per-token pricing, but a higher value concentrates the same demand into a larger upstream burst.

## Limitations

The fake model uses a fixed delay and cannot reproduce real first-token latency, generation speed, internet jitter, or provider-side queues. Results characterize this PC and the local EduGate path, not every teacher computer. A small real-API trial should still confirm HTTP 429 behavior and latency for the selected provider. Raw JSON and isolated databases remain locally under `artifacts/model-concurrency-2026-08-10` and are intentionally not committed.
