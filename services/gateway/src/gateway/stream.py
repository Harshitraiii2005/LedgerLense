"""Fan-out of flagged transactions from Kafka to WebSocket clients."""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from ledgerlens_common.models import RiskAssessment

log = logging.getLogger("gateway.stream")


class Broadcaster:
    """One bounded queue per connected client. A slow client loses its oldest messages
    instead of blocking the Kafka consumer or growing memory without limit."""

    def __init__(self, queue_size: int = 100) -> None:
        self.queue_size = queue_size
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.dropped = 0

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(self.queue_size)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self.subscribers.discard(q)

    def publish(self, message: str) -> None:
        for q in self.subscribers:
            if q.full():
                q.get_nowait()  # drop oldest
                self.dropped += 1
            q.put_nowait(message)


def make_consumer(bootstrap: str, topic: str) -> AIOKafkaConsumer:
    # group_id=None: no consumer group, so every gateway replica is assigned every partition
    # and each replica's clients see every flagged event. Nothing to commit; a restarted
    # replica starts from "latest" because a live feed doesn't replay history.
    return AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="latest",
    )


async def consume_flagged(consumer: AIOKafkaConsumer, broadcaster: Broadcaster) -> None:
    async for msg in consumer:
        try:
            assessment = RiskAssessment.model_validate_json(msg.value)
        except ValidationError:
            log.warning("skipping malformed flagged event", extra={"fields": {"offset": msg.offset}})
            continue
        if assessment.flagged:
            broadcaster.publish(assessment.model_dump_json())
