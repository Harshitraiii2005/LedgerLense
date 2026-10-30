"""Generator: publishes synthetic transactions to Kafka at a configurable rate."""

import asyncio
import logging
import random
import signal

from aiokafka import AIOKafkaProducer
from pydantic_settings import BaseSettings

from generator.patterns import TransactionFactory
from ledgerlens_common import TOPIC_RAW
from ledgerlens_common.logging import REQUEST_ID_HEADER, configure_logging, new_request_id

log = logging.getLogger("generator")


class Settings(BaseSettings):
    kafka_bootstrap: str = "localhost:9092"
    gen_rate_per_sec: float = 20.0
    gen_max_events: int = 0  # 0 = run forever
    gen_seed: int | None = None
    gen_suspicious_rate: float = 0.03
    # Small share of deliberately broken payloads, so the DLQ path is exercised end to end.
    gen_malformed_rate: float = 0.002
    log_level: str = "INFO"


async def produce(settings: Settings, stop: asyncio.Event) -> int:
    factory = TransactionFactory(settings.gen_seed, suspicious_rate=settings.gen_suspicious_rate)
    rng = random.Random(settings.gen_seed)
    # acks=all + idempotence: a broker retry can't duplicate or drop a message.
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap, acks="all", enable_idempotence=True
    )
    await producer.start()
    sent = 0
    interval = 1.0 / settings.gen_rate_per_sec
    loop = asyncio.get_running_loop()
    next_tick = loop.time()
    try:
        while not stop.is_set() and (settings.gen_max_events == 0 or sent < settings.gen_max_events):
            for txn, pattern in factory.next_batch():
                value = txn.model_dump_json().encode()
                if rng.random() < settings.gen_malformed_rate:
                    value = value.replace(b'"amount":"', b'"amount":"-')  # negative amount -> DLQ
                headers = [(REQUEST_ID_HEADER, new_request_id().encode())]
                if pattern:
                    headers.append(("x-synthetic-pattern", pattern.encode()))
                # Keyed by customer: all of a customer's txns land on one partition, in order.
                await producer.send(TOPIC_RAW, value, key=txn.customer_id.encode(), headers=headers)
                sent += 1
                # Pace against a fixed schedule, so slow sends don't lower the rate.
                next_tick += interval
                await asyncio.sleep(max(0.0, next_tick - loop.time()))
                if sent % 1000 == 0:
                    log.info("progress", extra={"fields": {"sent": sent}})
        await producer.flush()
    finally:
        await producer.stop()
    log.info("stopped", extra={"fields": {"sent": sent}})
    return sent


def run() -> None:
    settings = Settings()
    configure_logging("generator", settings.log_level)

    async def main() -> None:
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await produce(settings, stop)

    asyncio.run(main())
