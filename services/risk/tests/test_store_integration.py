"""ProfileStore against a real Couchbase node.

Couchbase clients bootstrap from the cluster map, which advertises the node's *real* ports,
so randomly mapped testcontainers ports don't work. Instead this test uses a node started
with fixed ports, and is skipped when COUCHBASE_TEST_URL is unset. CI starts one; locally:
  docker run -d --name cb -p 8091-8097:8091-8097 -p 11210:11210 couchbase/server:community-8.0.0
  docker cp services/risk/couchbase-init.sh cb:/init.sh
  docker exec -e CB_USER=Administrator -e CB_PASSWORD=change-me-local cb sh /init.sh
  COUCHBASE_TEST_URL=couchbase://localhost uv run pytest services/risk
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from ledgerlens_common.models import Transaction
from risk.config import Settings
from risk.store import ProfileStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif("COUCHBASE_TEST_URL" not in os.environ, reason="needs COUCHBASE_TEST_URL"),
]
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def store():
    cfg = Settings(
        couchbase_url=os.environ.get("COUCHBASE_TEST_URL", ""),
        couchbase_user=os.environ.get("COUCHBASE_TEST_USER", "Administrator"),
        couchbase_password=SecretStr(os.environ.get("COUCHBASE_TEST_PASSWORD", "change-me-local")),
    )
    s = ProfileStore(cfg)
    s.ensure_index()
    return s


def txn(customer: str, i: int, country: str = "US", amount: str = "40.00") -> Transaction:
    return Transaction(
        txn_id=uuid.uuid4().hex, customer_id=customer, merchant="Acme", category="travel",
        amount=Decimal(amount), currency="USD", country=country, channel="online",
        timestamp=T0 + timedelta(hours=i),
    )  # fmt: skip


def fresh_customer() -> str:
    return f"C{uuid.uuid4().int % 10**6:06d}"


def test_concurrent_writers_lose_no_updates(store):
    """8 threads hammer one profile; CAS retries must make every update count exactly once."""
    cust = fresh_customer()
    txns = [txn(cust, i) for i in range(40)]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(store.score_and_update, txns))
    profile, _ = store.get(cust)
    assert profile["txn_count"] == 40


def test_redelivery_is_idempotent(store):
    cust = fresh_customer()
    t = txn(cust, 0)
    first = store.score_and_update(t)
    again = store.score_and_update(t)
    profile, _ = store.get(cust)
    assert profile["txn_count"] == 1
    assert again.score == first.score


def test_dry_run_writes_nothing_and_report_lists_flagged(store):
    cust = fresh_customer()
    store.score_and_update(txn(cust, 0, "US"))
    jump = txn(cust, 0, "SG")
    jump = jump.model_copy(update={"timestamp": T0 + timedelta(minutes=10)})

    assert store.dry_run(jump).flagged
    assert store.get(cust)[0]["txn_count"] == 1

    assert store.score_and_update(jump).flagged
    # Large limit: the bucket may hold other customers' profiles (e.g. from a running stack).
    rows = store.top_risky(1_000_000)
    assert any(r["customer_id"] == cust and r["flagged_count"] == 1 for r in rows)
    keys = [(r["flagged_count"], r["max_score"]) for r in rows]
    assert keys == sorted(keys, reverse=True)
