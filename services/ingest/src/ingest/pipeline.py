"""Kafka -> validate -> MongoDB -> transactions.validated.

Delivery guarantee: at-least-once with idempotent writes.
  1. validate every record in the batch (bad ones are routed to the DLQ)
  2. write good ones to Mongo (_id = txn_id, so a redelivered message is a no-op)
  3. publish validated + DLQ messages and wait for broker acks
  4. only then commit the Kafka offset
A crash anywhere before step 4 means the batch is re-read and safely re-processed.
Downstream consumers of transactions.validated must likewise be idempotent on txn_id.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord
from bson import Decimal128
from ledgerlens_common import TOPIC_DLQ, TOPIC_VALIDATED
from ledgerlens_common.logging import REQUEST_ID_HEADER, request_id_var
from ledgerlens_common.models import Transaction
from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import ValidationError
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import BulkWriteError

log = logging.getLogger("ingest.pipeline")

DUPLICATE_KEY = 11000
# Serve "a customer's recent transactions" and "a merchant's recent transactions".
INDEXES = [
    IndexModel([("customer_id", ASCENDING), ("timestamp", DESCENDING)], name="customer_ts"),
    IndexModel([("merchant", ASCENDING), ("timestamp", DESCENDING)], name="merchant_ts"),
]


class InvalidMessage(Exception):
    def __init__(self, reason: str, detail: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def parse(raw: bytes | None) -> Transaction:
    try:
        payload = json.loads(raw or b"")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise InvalidMessage("invalid_json", str(e)) from None
    try:
        return Transaction.model_validate(payload)
    except ValidationError as e:
        errors = [{"loc": ".".join(map(str, err["loc"])), "msg": err["msg"]} for err in e.errors()]
        raise InvalidMessage("schema_validation_failed", errors) from None


def to_document(txn: Transaction, headers: dict[str, str]) -> dict[str, Any]:
    doc = txn.model_dump(exclude={"txn_id"})
    doc["_id"] = txn.txn_id
    doc["amount"] = Decimal128(str(txn.amount))  # exact decimal in BSON, not a float
    doc["ingested_at"] = datetime.now(UTC)
    if pattern := headers.get("x-synthetic-pattern"):
        doc["synthetic_pattern"] = pattern  # ground-truth label, for evaluating the risk rules
    return doc


async def ensure_indexes(collection: AsyncIOMotorCollection[dict[str, Any]]) -> None:
    await collection.create_indexes(INDEXES)


async def insert_idempotent(
    collection: AsyncIOMotorCollection[dict[str, Any]], docs: list[dict[str, Any]]
) -> int:
    """Insert docs; duplicates (already ingested on a previous delivery) are ignored."""
    try:
        result = await collection.insert_many(docs, ordered=False)
        return len(result.inserted_ids)
    except BulkWriteError as e:
        others = [err for err in e.details["writeErrors"] if err["code"] != DUPLICATE_KEY]
        if others:
            raise
        return int(e.details["nInserted"])


class Ingestor:
    def __init__(
        self,
        consumer: AIOKafkaConsumer,
        producer: AIOKafkaProducer,
        collection: AsyncIOMotorCollection[dict[str, Any]],
    ) -> None:
        self.consumer = consumer
        self.producer = producer
        self.collection = collection
        self.stats = {"inserted": 0, "dlq": 0, "duplicates": 0}

    async def handle(self, records: list[ConsumerRecord[bytes, bytes]]) -> None:
        docs: list[dict[str, Any]] = []
        outbound: list[tuple[str, bytes, bytes | None, list[tuple[str, bytes]]]] = []
        for r in records:
            raw_headers = list(r.headers)
            headers = {k: v.decode() for k, v in raw_headers}
            request_id_var.set(headers.get(REQUEST_ID_HEADER))
            try:
                txn = parse(r.value)
            except InvalidMessage as e:
                dead = {
                    "reason": e.reason,
                    "detail": e.detail,
                    "raw": (r.value or b"").decode(errors="replace"),
                    "source": {"topic": r.topic, "partition": r.partition, "offset": r.offset},
                    "failed_at": datetime.now(UTC).isoformat(),
                }
                outbound.append((TOPIC_DLQ, json.dumps(dead).encode(), r.key, raw_headers))
                log.warning("dead-lettered", extra={"fields": {"reason": e.reason, "offset": r.offset}})
                continue
            docs.append(to_document(txn, headers))
            outbound.append((TOPIC_VALIDATED, txn.model_dump_json().encode(), r.key, raw_headers))
        request_id_var.set(None)

        if docs:
            inserted = await insert_idempotent(self.collection, docs)
            self.stats["inserted"] += inserted
            self.stats["duplicates"] += len(docs) - inserted
        self.stats["dlq"] += len(records) - len(docs)
        # send() only enqueues; gathering the returned futures waits for broker acks.
        futures = [await self.producer.send(t, v, key=k, headers=h) for t, v, k, h in outbound]
        await asyncio.gather(*futures)

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            batches = await self.consumer.getmany(timeout_ms=1000, max_records=500)
            for tp, records in batches.items():
                try:
                    await self.handle(records)
                except Exception:
                    # Don't commit; rewind so the same records are retried after a pause.
                    log.exception("batch failed, will retry", extra={"fields": {"backoff_s": backoff}})
                    self.consumer.seek(tp, records[0].offset)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                await self.consumer.commit({tp: records[-1].offset + 1})
                backoff = 1.0
