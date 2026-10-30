# Risk throughput — 2026-10-30T14:54:14.615681+00:00

Command: `make bench-risk`. Setup: Apple Silicon 10 CPU / 16 GB laptop; Couchbase Server community-8.0.0 single node in Docker Desktop (8 GB), 512 MB data quota; one thread.

| measurement | txns | txns/s | per-txn p50 / p95 / p99 ms |
|---|---|---|---|
| A. rules engine only, in-process (no I/O, upper bound) | 100000 | 94164 | — |
| B. score_and_update: Couchbase KV get + CAS write, serial | 10000 | 2167 | 0.407 / 0.77 / 1.116 |
