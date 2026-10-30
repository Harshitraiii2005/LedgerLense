"""Risk rules vs. ground truth: confusion matrix of risk.flagged vs the synthetic_pattern label.

Transaction-level. Note two ceilings by design: a velocity burst's first few txns can't be
flagged (the burst hasn't happened yet), and an impossible-geography pair's first leg is normal.
Usage:  MONGO_URI=... uv run python benchmarks/risk_eval.py
Writes: benchmarks/results/risk_eval.json and risk_eval.md
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pymongo import MongoClient

RESULTS = Path(__file__).parent / "results"


def main() -> None:
    client: MongoClient[dict[str, Any]] = MongoClient(os.environ["MONGO_URI"])
    t = client["ledgerlens"]["transactions"]
    rows = t.aggregate(
        [
            {"$match": {"risk": {"$exists": True}}},
            {
                "$group": {
                    "_id": {
                        "pattern": {"$ifNull": ["$synthetic_pattern", "none"]},
                        "flagged": "$risk.flagged",
                    },
                    "n": {"$sum": 1},
                }
            },
        ]
    )
    cells = {(r["_id"]["pattern"], r["_id"]["flagged"]): r["n"] for r in rows}
    patterns = sorted({p for p, _ in cells} - {"none"})
    tp = sum(cells.get((p, True), 0) for p in patterns)
    fn = sum(cells.get((p, False), 0) for p in patterns)
    fp = cells.get(("none", True), 0)
    tn = cells.get(("none", False), 0)
    fp_reasons = {
        "+".join(r["_id"]): r["n"]
        for r in t.aggregate(
            [
                {"$match": {"risk.flagged": True, "synthetic_pattern": {"$exists": False}}},
                {"$group": {"_id": "$risk.reasons.code", "n": {"$sum": 1}}},
            ]
        )
    }
    result = {
        "run_at": datetime.now(UTC).isoformat(),
        "scored": tp + fn + fp + tn,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "recall_by_pattern": {
            p: round(cells.get((p, True), 0) / (cells.get((p, True), 0) + cells.get((p, False), 0)), 4)
            for p in patterns
        },
        "false_positive_reasons": fp_reasons,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "risk_eval.json").write_text(json.dumps(result, indent=2))
    md = [
        f"# Risk rules vs synthetic labels — {result['scored']} scored txns",
        f"Run at {result['run_at']}. Command: `MONGO_URI=... uv run python benchmarks/risk_eval.py`\n",
        f"precision **{result['precision']}**, recall **{result['recall']}** "
        f"(tp={tp}, fp={fp}, fn={fn}, tn={tn})\n",
        "| pattern | recall |",
        "|---|---|",
        *[f"| {p} | {r} |" for p, r in result["recall_by_pattern"].items()],
        "\nFalse positives by reason: " + json.dumps(fp_reasons),
    ]
    (RESULTS / "risk_eval.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
