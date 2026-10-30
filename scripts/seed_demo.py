"""Seed a MongoDB (e.g. the Atlas demo cluster) with synthetic, already-scored transactions.

The cloud demo has data immediately, before the generator -> ingest -> risk pipeline has run.
Usage:  MONGO_URI=... uv run python scripts/seed_demo.py [--docs 5000] [--db ledgerlens]
Idempotent: _id = txn_id and the seed is fixed, so re-running only re-upserts the same docs.
"""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReplaceOne

from ingest.pipeline import INDEXES

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
from agent_eval import seed_records


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=5000)
    ap.add_argument("--db", default="ledgerlens")
    args = ap.parse_args()

    coll = MongoClient(os.environ["MONGO_URI"])[args.db]["transactions"]
    recs = seed_records(args.docs, datetime.now(UTC).replace(microsecond=0))
    coll.bulk_write([ReplaceOne({"_id": r["_id"]}, r, upsert=True) for r in recs], ordered=False)
    coll.create_indexes(
        [*INDEXES, IndexModel([("risk.flagged", ASCENDING), ("timestamp", DESCENDING)], name="flagged_ts")]
    )
    flagged = coll.count_documents({"risk.flagged": True})
    print(f"{args.db}.transactions: {coll.estimated_document_count()} docs, {flagged} flagged")
    print("indexes:", sorted(coll.index_information()))


if __name__ == "__main__":
    main()
