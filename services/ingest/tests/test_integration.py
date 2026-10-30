"""End-to-end: generator -> real Kafka -> ingest app -> real MongoDB (testcontainers, needs Docker)."""

import asyncio
import socket
from collections.abc import Iterator

import httpx
import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from motor.motor_asyncio import AsyncIOMotorClient
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from generator.main import Settings as GenSettings
from generator.main import produce
from ingest.app import Settings as IngestSettings
from ingest.app import create_app
from ingest.pipeline import insert_idempotent
from ledgerlens_common import TOPIC_DLQ, TOPIC_RAW, TOPIC_VALIDATED

pytestmark = pytest.mark.integration


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    with MongoDbContainer("mongo:8.0.34") as mongo:
        yield mongo.get_connection_url()


@pytest.fixture(scope="session")
def kafka_bootstrap() -> Iterator[str]:
    # Same image as docker-compose. The host port must be known before start,
    # because the broker advertises it to clients.
    port = free_port()
    kafka = (
        DockerContainer("apache/kafka:3.9.1")
        .with_bind_ports(9092, port)
        .with_envs(
            KAFKA_NODE_ID="1",
            KAFKA_PROCESS_ROLES="broker,controller",
            KAFKA_LISTENERS="PLAINTEXT://:9092,CONTROLLER://:9093",
            KAFKA_ADVERTISED_LISTENERS=f"PLAINTEXT://localhost:{port}",
            KAFKA_LISTENER_SECURITY_PROTOCOL_MAP="CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            KAFKA_CONTROLLER_QUORUM_VOTERS="1@localhost:9093",
            KAFKA_CONTROLLER_LISTENER_NAMES="CONTROLLER",
            KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR="1",
            KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR="1",
            KAFKA_TRANSACTION_STATE_LOG_MIN_ISR="1",
            KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS="0",
        )
    )
    with kafka:
        wait_for_logs(kafka, "Kafka Server started", timeout=90)
        yield f"localhost:{port}"


PARTITIONS = 3


async def topic_size(bootstrap: str, topic: str) -> int:
    c = AIOKafkaConsumer(bootstrap_servers=bootstrap)
    await c.start()
    try:
        tps = [TopicPartition(topic, p) for p in range(PARTITIONS)]
        return sum((await c.end_offsets(tps)).values())
    finally:
        await c.stop()


async def test_insert_is_idempotent(mongo_url):
    coll = AsyncIOMotorClient(mongo_url)["it"]["dupes"]
    docs = [{"_id": "t1"}, {"_id": "t2"}]
    assert await insert_idempotent(coll, docs) == 2
    assert await insert_idempotent(coll, [*docs, {"_id": "t3"}]) == 1  # redelivery + one new


async def test_end_to_end(mongo_url, kafka_bootstrap):
    admin = AIOKafkaAdminClient(bootstrap_servers=kafka_bootstrap)
    await admin.start()
    await admin.create_topics([NewTopic(t, PARTITIONS, 1) for t in (TOPIC_RAW, TOPIC_VALIDATED, TOPIC_DLQ)])
    await admin.close()

    sent = await produce(
        GenSettings(
            kafka_bootstrap=kafka_bootstrap,
            gen_rate_per_sec=2000,
            gen_max_events=300,
            gen_seed=1,
            gen_malformed_rate=0.05,
        ),
        asyncio.Event(),
    )
    assert sent >= 300

    app = create_app(IngestSettings(kafka_bootstrap=kafka_bootstrap, mongo_uri=mongo_url, mongo_db="e2e"))
    async with app.router.lifespan_context(app):
        coll = app.state.mongo["e2e"]["transactions"]
        stats = app.state.ingestor.stats
        for _ in range(60):
            if stats["inserted"] + stats["dlq"] >= sent:
                break
            await asyncio.sleep(0.5)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
            ready = await http.get("/health/ready")
        assert ready.status_code == 200, ready.text

        n_docs = await coll.count_documents({})
        indexes = await coll.index_information()
    # Leaving the context = graceful shutdown (finish batch, commit, close clients).

    assert stats["dlq"] > 0, "malformed messages should be dead-lettered"
    assert n_docs + stats["dlq"] == sent
    assert {"customer_ts", "merchant_ts"} <= set(indexes)
    assert await topic_size(kafka_bootstrap, TOPIC_VALIDATED) == n_docs
    assert await topic_size(kafka_bootstrap, TOPIC_DLQ) == stats["dlq"]
