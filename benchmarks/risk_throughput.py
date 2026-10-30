"""Risk benchmark, two clearly separated measurements:

  A. rules engine only, in-process: assess() + apply() on in-memory profiles. Pure CPU, no I/O.
     An upper bound, not what the service achieves.
  B. ProfileStore.score_and_update() against a real Couchbase node: per txn one KV get +
     one CAS insert/replace, done serially (the risk consumer scores a batch serially too).
     This is the dominant per-transaction cost of the consumer; Kafka and the Mongo
     bulk_write (one per batch) are not included. Skipped unless COUCHBASE_URL is set.

Usage:  [COUCHBASE_URL=couchbase://localhost COUCHBASE_PASSWORD=...] \
        uv run python -u benchmarks/risk_throughput.py
Writes: benchmarks/results/risk_throughput.{json,md}
"""

import argparse
import json
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from generator.patterns import TransactionFactory
from ledgerlens_common.models import Transaction
from risk.config import Settings
from risk.rules import Profile, apply, assess, new_profile

RESULTS = Path(__file__).parent / "results"


def stream(n: int) -> list[Transaction]:
    """n generator txns on a 50 ms timeline (20 txn/s, like the default generator rate)."""
    factory, t0, out = TransactionFactory(seed=3), datetime(2026, 10, 1, tzinfo=UTC), []
    while len(out) < n:
        out.extend(t for t, _ in factory.next_batch(t0 + timedelta(milliseconds=50 * len(out))))
    return out[:n]


def pct(values: list[float], p: int) -> float:
    return round(statistics.quantiles(values, n=100)[p - 1], 3)


def bench_rules(txns: list[Transaction], cfg: Settings) -> dict[str, Any]:
    profiles: dict[str, Profile] = {}
    flagged = 0
    t0 = time.perf_counter()
    for txn in txns:
        p = profiles.get(txn.customer_id) or new_profile(txn.customer_id)
        a = assess(txn, p, cfg)
        profiles[txn.customer_id] = apply(p, a)
        flagged += a.flagged
    wall = time.perf_counter() - t0
    return {"txns": len(txns), "txns_per_s": round(len(txns) / wall), "flagged": flagged}


def bench_couchbase(txns: list[Transaction], cfg: Settings) -> dict[str, Any]:
    from risk.store import ProfileStore  # imported here: needs the Couchbase SDK + a reachable node

    store = ProfileStore(cfg)
    per_txn: list[float] = []
    t0 = time.perf_counter()
    for txn in txns:
        s = time.perf_counter()
        store.score_and_update(txn)
        per_txn.append((time.perf_counter() - s) * 1000)
    wall = time.perf_counter() - t0
    return {
        "txns": len(txns),
        "txns_per_s": round(len(txns) / wall),
        "p50_ms": pct(per_txn, 50),
        "p95_ms": pct(per_txn, 95),
        "p99_ms": pct(per_txn, 99),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules-n", type=int, default=100_000)
    ap.add_argument("--couchbase-n", type=int, default=10_000)
    args = ap.parse_args()
    cfg = Settings(
        couchbase_url=os.environ.get("COUCHBASE_URL", ""),
        couchbase_password=SecretStr(os.environ.get("COUCHBASE_PASSWORD", "")),
    )

    out: dict[str, Any] = {
        "run_at": datetime.now(UTC).isoformat(),
        "setup": "Apple Silicon 10 CPU / 16 GB laptop; Couchbase Server community-8.0.0 "
        "single node in Docker Desktop (8 GB), 512 MB data quota; one thread",
    }
    out["rules_in_process"] = bench_rules(stream(args.rules_n), cfg)
    print("A rules engine (in-process, no I/O):", json.dumps(out["rules_in_process"]))
    if cfg.couchbase_url:
        out["couchbase_score_and_update"] = bench_couchbase(stream(args.couchbase_n), cfg)
        print(
            "B score_and_update (Couchbase KV get + CAS write):",
            json.dumps(out["couchbase_score_and_update"]),
        )

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "risk_throughput.json").write_text(json.dumps(out, indent=2))
    a = out["rules_in_process"]
    lines = [
        f"# Risk throughput — {out['run_at']}",
        "",
        f"Command: `make bench-risk`. Setup: {out['setup']}.",
        "",
        "| measurement | txns | txns/s | per-txn p50 / p95 / p99 ms |",
        "|---|---|---|---|",
        f"| A. rules engine only, in-process (no I/O, upper bound) | {a['txns']} | {a['txns_per_s']} | — |",
    ]
    if b := out.get("couchbase_score_and_update"):
        lines.append(
            f"| B. score_and_update: Couchbase KV get + CAS write, serial | {b['txns']} | "
            f"{b['txns_per_s']} | {b['p50_ms']} / {b['p95_ms']} / {b['p99_ms']} |"
        )
    (RESULTS / "risk_throughput.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
