"""transactions.validated -> score -> Couchbase profile + Mongo `risk` field -> transactions.flagged.

Flask is synchronous, so the Kafka consumer runs in its own thread with its own asyncio
event loop (aiokafka, same client as ingest). Processing inside that loop is sequential
and uses the blocking Couchbase/pymongo clients, which is fine: this thread does nothing else.

Same delivery contract as ingest: side effects first (all idempotent on txn_id), commit last.
"""

import asyncio
import logging
import threading
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRecord
from pydantic import ValidationError
from pymongo import UpdateOne
from pymongo.collection import Collection

from ledgerlens_common import TOPIC_FLAGGED, TOPIC_VALIDATED
from ledgerlens_common.logging import REQUEST_ID_HEADER, request_id_var
from ledgerlens_common.models import RiskAssessment, Transaction
from risk.config import Settings
from risk.store import ProfileStore

log = logging.getLogger("risk.consumer")


def risk_subdoc(a: RiskAssessment) -> dict[str, Any]:
    """The `risk` object stored on the Mongo transaction (and served by gateway /v2)."""
    return {
        "score": a.score,
        "flagged": a.flagged,
        "reasons": [r.model_dump() for r in a.reasons],
        "scored_at": a.scored_at,
    }


class RiskConsumer:
    def __init__(self, cfg: Settings, store: ProfileStore, transactions: Collection[dict[str, Any]]) -> None:
        self.cfg = cfg
        self.store = store
        self.transactions = transactions
        self.stats = {"scored": 0, "flagged": 0, "invalid": 0, "missing_in_mongo": 0}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self.thread = threading.Thread(target=self._thread_main, name="risk-consumer", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 15) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)  # asyncio objects aren't thread-safe
        self.thread.join(timeout)

    def alive(self) -> bool:
        return self.thread.is_alive()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            log.exception("consumer thread crashed")  # readiness turns red via alive()

    def handle(self, records: list[ConsumerRecord[bytes, bytes]]) -> list[tuple[RiskAssessment, Any]]:
        """Score a batch and update Mongo. Returns the flagged assessments still to publish."""
        updates: list[UpdateOne] = []
        to_publish: list[tuple[RiskAssessment, Any]] = []
        for r in records:
            headers = list(r.headers)
            request_id_var.set(dict(headers).get(REQUEST_ID_HEADER, b"").decode() or None)
            try:
                txn = Transaction.model_validate_json(r.value or b"")
            except ValidationError:
                # ingest already validated this; a failure here is a contract bug, not bad input.
                log.error("invalid validated event, skipping", extra={"fields": {"offset": r.offset}})
                self.stats["invalid"] += 1
                continue
            a = self.store.score_and_update(txn)
            updates.append(UpdateOne({"_id": txn.txn_id}, {"$set": {"risk": risk_subdoc(a)}}))
            self.stats["scored"] += 1
            if a.flagged:
                self.stats["flagged"] += 1
                to_publish.append((a, headers))
                log.info(
                    "flagged",
                    extra={
                        "fields": {
                            "txn_id": a.txn_id,
                            "score": a.score,
                            "reasons": [x.code for x in a.reasons],
                        }
                    },
                )
        request_id_var.set(None)
        if updates:
            # No upsert: ingest writes the txn before publishing it, so it must exist.
            res = self.transactions.bulk_write(updates, ordered=False)
            self.stats["missing_in_mongo"] += len(updates) - res.matched_count
        return to_publish

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        consumer = AIOKafkaConsumer(
            TOPIC_VALIDATED,
            bootstrap_servers=self.cfg.kafka_bootstrap,
            group_id=self.cfg.consumer_group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=self.cfg.kafka_bootstrap, acks="all", enable_idempotence=True
        )
        await producer.start()
        await consumer.start()
        log.info("risk consumer started")
        backoff = 1.0
        try:
            while not self._stop.is_set():
                batches = await consumer.getmany(timeout_ms=1000, max_records=200)
                for tp, records in batches.items():
                    try:
                        flagged = self.handle(records)
                        futures = [
                            await producer.send(
                                TOPIC_FLAGGED,
                                a.model_dump_json().encode(),
                                key=a.customer_id.encode(),
                                headers=h,
                            )
                            for a, h in flagged
                        ]
                        await asyncio.gather(*futures)  # wait for broker acks before committing
                    except Exception:
                        log.exception("batch failed, will retry", extra={"fields": {"backoff_s": backoff}})
                        consumer.seek(tp, records[0].offset)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    await consumer.commit({tp: records[-1].offset + 1})
                    backoff = 1.0
        finally:
            await consumer.stop()
            await producer.stop()
            log.info("risk consumer stopped", extra={"fields": self.stats})
