"""RiskConsumer.handle with in-memory fakes: Mongo update shape, publish list, stats."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from risk.config import Settings
from risk.consumer import RiskConsumer
from risk.rules import apply, assess, new_profile

CFG = Settings()
BASE = {
    "customer_id": "C000001", "merchant": "Acme", "category": "travel", "amount": "40.00",
    "currency": "USD", "channel": "online",
}  # fmt: skip
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Record:
    value: bytes | None
    offset: int
    headers: tuple = (("x-request-id", b"rid-1"),)


class FakeStore:
    """Same semantics as ProfileStore.score_and_update, backed by a dict."""

    def __init__(self):
        self.profiles = {}

    def score_and_update(self, txn):
        p = self.profiles.get(txn.customer_id) or new_profile(txn.customer_id)
        a = assess(txn, p, CFG)
        self.profiles[txn.customer_id] = apply(p, a)
        return a


@dataclass
class BulkResult:
    matched_count: int


class FakeTransactions:
    def __init__(self, existing):
        self.existing = existing
        self.ops = []

    def bulk_write(self, ops, ordered):
        self.ops.extend(ops)
        return BulkResult(sum(1 for op in ops if op._filter["_id"] in self.existing))


def record(txn_id, country, minutes, offset):
    ts = (T0 + timedelta(minutes=minutes)).isoformat()
    return Record(json.dumps(BASE | {"txn_id": txn_id, "country": country, "timestamp": ts}).encode(), offset)


def test_handle_scores_updates_mongo_and_returns_flagged():
    mongo = FakeTransactions(existing={"txn00000001", "txn00000002"})
    c = RiskConsumer(CFG, FakeStore(), mongo)  # type: ignore[arg-type]
    out = c.handle([
        record("txn00000001", "US", 0, 0),
        record("txn00000002", "SG", 10, 1),  # country jump -> flagged
        record("txn00000003", "US", 20, 2),  # not in Mongo (shouldn't happen) -> counted
        Record(b"{not json", 3),
    ])  # fmt: skip

    assert [a.txn_id for a, _ in out] == ["txn00000002"]
    assert out[0][1] == [("x-request-id", b"rid-1")]  # headers forwarded as-is
    assert len(mongo.ops) == 3
    flagged_update = mongo.ops[1]._doc["$set"]["risk"]
    assert flagged_update["flagged"] is True
    assert flagged_update["reasons"][0]["code"] == "COUNTRY_JUMP"
    assert c.stats == {"scored": 3, "flagged": 1, "invalid": 1, "missing_in_mongo": 1}
