# Ingest throughput — 2026-10-30T14:52:16.325009+00:00

Command: `make bench-ingest`. Setup: single-node Kafka 3.9.1 (6 partitions), MongoDB 8.0.34, Docker Desktop 8 GB, Apple Silicon 10 CPU / 16 GB laptop; one Ingestor process; live stack running alongside.

| metric | value |
|---|---|
| producer only (acks=all, idempotent) | 91648 msg/s |
| ingest drain (50000 backlog -> Mongo + validated topic) | 32145 msg/s |
| end-to-end latency (send -> validated event received) @ 500 msg/s, 10000 msgs | p50 1.4 ms, p95 2.8 ms, p99 15.3 ms, max 63.2 ms |
