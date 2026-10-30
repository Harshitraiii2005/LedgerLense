"""Transformation tests on a local SparkSession. Run in the Spark image: `make test-spark`.

Skipped automatically on hosts without pyspark/Java (e.g. the root `uv run pytest`).
"""

import shutil
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("pyspark")
if not shutil.which("java"):
    pytest.skip("Java not available", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aggregates import (
    TRANSACTIONS_SCHEMA,
    daily_category_spend,
    merchant_flagged_rate,
    top_customers,
)
from pyspark.sql import SparkSession


@pytest.fixture(scope="module")
def spark():
    s = (
        SparkSession.builder.master("local[1]")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    yield s
    s.stop()


def txn(i, customer, merchant, amount, currency="USD", day=1, category="groceries", flagged=None):
    risk = None if flagged is None else (50, flagged, [])
    return (f"t{i}", customer, merchant, category, Decimal(amount), currency, "US", "online",
            datetime(2026, 1, day, 12), risk)  # fmt: skip


@pytest.fixture(scope="module")
def txns(spark):
    rows = [
        txn(1, "C000001", "Acme", "10.10", day=1),
        txn(2, "C000001", "Acme", "0.20", day=1, flagged=True),
        txn(3, "C000002", "Acme", "5.00", currency="JPY", day=1),
        txn(4, "C000002", "Bobs", "7.00", day=2, category="fuel", flagged=False),
        txn(5, "C000003", "Bobs", "1.00", day=2, category="fuel"),
        txn(6, "C000001", "Bobs", "2.00", currency="EUR", day=2, category="fuel", flagged=True),
    ]
    return spark.createDataFrame(rows, TRANSACTIONS_SCHEMA)


def test_daily_category_spend_never_mixes_currencies(spark, txns):
    rows = {(str(r.day), r.category, r.currency): (r.txn_count, r.total_amount)
            for r in daily_category_spend(spark, txns).collect()}  # fmt: skip
    assert rows == {
        ("2026-01-01", "groceries", "USD"): (2, Decimal("10.30")),  # exact decimal, no float drift
        ("2026-01-01", "groceries", "JPY"): (1, Decimal("5.00")),
        ("2026-01-02", "fuel", "USD"): (2, Decimal("8.00")),
        ("2026-01-02", "fuel", "EUR"): (1, Decimal("2.00")),
    }


def test_merchant_flagged_rate_treats_unscored_as_not_flagged(txns):
    rows = {r.merchant: (r.total_txns, r.flagged_txns, r.flagged_rate)
            for r in merchant_flagged_rate(txns).collect()}  # fmt: skip
    assert rows == {"Acme": (3, 1, 0.3333), "Bobs": (3, 1, 0.3333)}


def test_top_customers_rank_and_amounts_per_currency(txns):
    rows = top_customers(txns, n=2).orderBy("rank").collect()
    assert [(r.customer_id, r.txn_count, r.rank) for r in rows] == [("C000001", 3, 1), ("C000002", 2, 2)]
    assert [(a.currency, a.total_amount) for a in rows[0].amounts] == [
        ("EUR", Decimal("2.00")),
        ("USD", Decimal("10.30")),
    ]
