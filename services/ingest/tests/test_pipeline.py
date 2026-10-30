"""Unit tests: validation, DLQ routing and the commit-after-write contract, with in-memory fakes."""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from bson import Decimal128
from ingest.pipeline import Ingestor, InvalidMessage, parse, to_document
from ledgerlens_common import TOPIC_DLQ, TOPIC_VALIDATED

VALID = {
    "txn_id": "a" * 32,
    "customer_id": "C000001",
    "merchant": "Acme Ltd",
    "category": "groceries",
    "amount": "12.30",
    "currency": "USD",
    "country": "US",
    "channel": "online",
    "timestamp": "2026-01-01T10:00:00Z",
}


def raw(**overrides) -> bytes:
    return json.dumps(VALID | overrides).encode()


def test_parse_valid():
    txn = parse(raw())
    assert txn.amount == Decimal("12.30")
    assert txn.timestamp == datetime(2026, 1, 1, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"not json", "invalid_json"),
        (b"\xff\xfe", "invalid_json"),
        (None, "invalid_json"),
        (raw(amount="-5.00"), "schema_validation_failed"),
        (raw(amount="1.234"), "schema_validation_failed"),  # sub-cent precision
        (raw(currency="XXX"), "schema_validation_failed"),
        (raw(timestamp="2026-01-01T10:00:00"), "schema_validation_failed"),  # naive datetime
        (raw(customer_id="bob"), "schema_validation_failed"),
        (raw(unexpected="field"), "schema_validation_failed"),
    ],
)
def test_parse_rejects(payload, reason):
    with pytest.raises(InvalidMessage) as exc:
        parse(payload)
    assert exc.value.reason == reason


def test_to_document_uses_txn_id_and_decimal128():
    doc = to_document(parse(raw()), {"x-synthetic-pattern": "amount_spike"})
    assert doc["_id"] == VALID["txn_id"]
    assert "txn_id" not in doc
    assert doc["amount"] == Decimal128("12.30")
    assert doc["synthetic_pattern"] == "amount_spike"


# --- fakes -----------------------------------------------------------------------------


@dataclass
class Record:
    value: bytes | None
    offset: int
    key: bytes = b"C000001"
    topic: str = "transactions.raw"
    partition: int = 0
    headers: tuple = (("x-request-id", b"rid-1"),)


@dataclass
class FakeProducer:
    sent: list = field(default_factory=list)

    async def send(self, topic, value, key=None, headers=None):
        self.sent.append((topic, json.loads(value)))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


@dataclass
class FakeResult:
    inserted_ids: list


@dataclass
class FakeCollection:
    fail: bool = False
    docs: list = field(default_factory=list)

    async def insert_many(self, docs, ordered):
        if self.fail:
            raise ConnectionError("mongo down")
        self.docs.extend(docs)
        return FakeResult([d["_id"] for d in docs])


async def test_handle_routes_good_and_bad():
    producer, coll = FakeProducer(), FakeCollection()
    ing = Ingestor(None, producer, coll)  # type: ignore[arg-type]
    await ing.handle([Record(raw(), 0), Record(b"{", 1), Record(raw(txn_id="b" * 32), 2)])

    assert [d["_id"] for d in coll.docs] == ["a" * 32, "b" * 32]
    topics = [t for t, _ in producer.sent]
    assert topics.count(TOPIC_VALIDATED) == 2
    assert topics.count(TOPIC_DLQ) == 1
    dead = next(v for t, v in producer.sent if t == TOPIC_DLQ)
    assert dead["reason"] == "invalid_json"
    assert dead["source"]["offset"] == 1
    assert ing.stats == {"inserted": 2, "dlq": 1, "duplicates": 0}


async def test_mongo_failure_publishes_nothing():
    # If the write fails, nothing may go downstream (and the caller won't commit).
    producer = FakeProducer()
    ing = Ingestor(None, producer, FakeCollection(fail=True))  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await ing.handle([Record(raw(), 0)])
    assert producer.sent == []
