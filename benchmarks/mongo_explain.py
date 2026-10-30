"""Index benchmark: explain("executionStats") for the two hot queries, without and with indexes.

Seeds a *separate* database (ledgerlens_bench) so it never touches live data.
Usage:  MONGO_URI=... uv run python benchmarks/mongo_explain.py [--docs 200000]
Writes: benchmarks/results/mongo_explain.json (raw explain output) and mongo_explain.md (summary).
"""

import argparse
import json
import os
import random
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import MongoClient

from generator.patterns import TransactionFactory
from ingest.pipeline import INDEXES, to_document

RESULTS = Path(__file__).parent / "results"


def seed(coll: Any, n: int) -> None:
    factory, rng = TransactionFactory(seed=42), random.Random(42)
    end = datetime(2026, 10, 1, tzinfo=UTC)
    docs: list[dict[str, Any]] = []
    inserted = 0
    while inserted + len(docs) < n:
        now = end - timedelta(seconds=rng.randrange(90 * 86400))  # spread over 90 days
        docs.extend(to_document(t, {}) for t, _ in factory.next_batch(now))
        if len(docs) >= 10_000:
            coll.insert_many(docs)
            inserted += len(docs)
            docs.clear()
    if docs:
        coll.insert_many(docs)


def queries(coll: Any) -> dict[str, tuple[dict[str, Any], list[tuple[str, int]], int]]:
    sample = coll.find_one({}, {"customer_id": 1, "merchant": 1})
    since = datetime(2026, 9, 24, tzinfo=UTC)
    return {
        # Risk scoring: a customer's latest 50 transactions.
        "customer_recent": ({"customer_id": sample["customer_id"]}, [("timestamp", -1)], 50),
        # Merchant view: one merchant's last 7 days.
        "merchant_last_7d": (
            {"merchant": sample["merchant"], "timestamp": {"$gte": since}},
            [("timestamp", -1)],
            0,
        ),
    }


def measure(coll: Any, flt: dict[str, Any], sort: list[tuple[str, int]], limit: int) -> dict[str, Any]:
    cursor = coll.find(flt).sort(sort).limit(limit)
    plan = cursor.explain()  # pymongo defaults to "allPlansExecution" verbosity, includes executionStats
    stats = plan["executionStats"]
    timings = []
    for _ in range(20):
        t0 = time.perf_counter()
        list(coll.find(flt).sort(sort).limit(limit))
        timings.append((time.perf_counter() - t0) * 1000)
    winning = plan["queryPlanner"]["winningPlan"]
    return {
        "summary": {
            "winning_stage": "IXSCAN" if '"IXSCAN"' in json.dumps(winning, default=str) else "COLLSCAN",
            "n_returned": stats["nReturned"],
            "total_docs_examined": stats["totalDocsExamined"],
            "total_keys_examined": stats["totalKeysExamined"],
            "execution_time_ms": stats["executionTimeMillis"],
            "client_median_ms_20_runs": round(statistics.median(timings), 2),
        },
        "explain": plan,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=None, help="defaults to $MONGO_URI")
    ap.add_argument("--docs", type=int, default=200_000)
    args = ap.parse_args()
    client: MongoClient[dict[str, Any]] = MongoClient(args.uri or os.environ["MONGO_URI"])
    coll = client["ledgerlens_bench"]["transactions"]
    coll.drop()
    t0 = time.perf_counter()
    seed(coll, args.docs)
    print(f"seeded {coll.estimated_document_count()} docs in {time.perf_counter() - t0:.1f}s")

    qs = queries(coll)
    results: dict[str, Any] = {"docs": args.docs, "mongo_version": client.server_info()["version"],
                               "run_at": datetime.now(UTC).isoformat(), "queries": {}}  # fmt: skip
    for phase in ("without_indexes", "with_indexes"):
        if phase == "with_indexes":
            coll.create_indexes(INDEXES)
        for name, (flt, sort, limit) in qs.items():
            results["queries"].setdefault(name, {})[phase] = measure(coll, flt, sort, limit)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "mongo_explain.json").write_text(json.dumps(results, indent=2, default=str))
    lines = [
        f"# Mongo index explain() — {args.docs} docs, MongoDB {results['mongo_version']}",
        f"Run at {results['run_at']}. Command: `make bench`. Raw output: mongo_explain.json\n",
        "| query | indexes | stage | docs examined | keys examined | returned | server ms | client p50 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, phases in results["queries"].items():
        for phase, r in phases.items():
            s = r["summary"]
            lines.append(f"| {name} | {phase} | {s['winning_stage']} | {s['total_docs_examined']} | "
                         f"{s['total_keys_examined']} | {s['n_returned']} | {s['execution_time_ms']} | "
                         f"{s['client_median_ms_20_runs']} |")  # fmt: skip
    (RESULTS / "mongo_explain.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    client.drop_database("ledgerlens_bench")


if __name__ == "__main__":
    main()
