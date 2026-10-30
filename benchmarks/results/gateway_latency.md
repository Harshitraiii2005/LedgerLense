# Gateway pagination latency — 2026-10-30T14:53:05.858127+00:00

Command: `make bench-gateway`. 58281 live transactions, page size 50, 400 requests per row at concurrency 16. Setup: one local gateway process (uvicorn, 1 worker) + compose MongoDB 8.0.34 in Docker Desktop (8 GB), load generator on the same Apple Silicon 10 CPU / 16 GB laptop.

| version | depth (rows skipped) | p50 ms | p95 ms | p99 ms | req/s |
|---|---|---|---|---|---|
| v1 offset | 0 | 26.6 | 40.08 | 59.66 | 552.3 |
| v2 cursor | 0 | 15.83 | 45.08 | 72.82 | 781.4 |
| v1 offset | 2450 | 34.1 | 56.09 | 65.49 | 436.6 |
| v2 cursor | 2450 | 20.16 | 65.63 | 106.29 | 584.1 |
| v1 offset | 9950 | 63.42 | 84.62 | 99.18 | 252.4 |
| v2 cursor | 9950 | 20.92 | 67.54 | 102.11 | 550.2 |
