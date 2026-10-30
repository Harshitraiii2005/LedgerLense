"""Ingest benchmark: producer rate, ingest drain throughput, and end-to-end latency.

Runs the real ingest code (ingest.pipeline.Ingestor) against the live Kafka + Mongo, but on
dedicated bench topics, a bench consumer group and a bench database, so the live stack is
untouched. Three measurements:
  1. producer-only: N generator txns sent as fast as possible (acks=all, idempotent)
  2. drain: the Ingestor consumes that backlog -> Mongo -> bench.validated; msgs/sec
  3. latency: a fixed-rate stream (below saturation) with the Ingestor running alongside.
     latency = time a downstream consumer receives the `validated` event (published only
     after the Mongo write is acknowledged) - txn timestamp (set at send time), so it covers
     produce -> Kafka -> ingest -> Mongo write -> publish -> Kafka -> consumer.

Usage:  MONGO_URI=... uv run python -u benchmarks/ingest_throughput.py [--n 50000]
Writes: benchmarks/results/ingest_throughput.{json,md}
"""

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from motor.motor_asyncio import AsyncIOMotorClient

import ingest.pipeline as pipeline
from generator.patterns import TransactionFactory

RESULTS = Path(__file__).parent / "results"
RUN = uuid.uuid4().hex[:8]
RAW, LAT, VALIDATED, LAT_VALIDATED, DLQ = (
    f"bench.{RUN}.{t}" for t in ("raw", "lat", "validated", "lat-validated", "dlq")
)
TOPICS = (RAW, LAT, VALIDATED, LAT_VALIDATED, DLQ)
PARTITIONS = 6

# Point the Ingestor's outbound topics at bench topics (it reads these module globals per batch),
# so bench events never reach the live transactions.validated consumers.
pipeline.TOPIC_VALIDATED, pipeline.TOPIC_DLQ = VALIDATED, DLQ


def pct(values: list[float], p: float) -> float:
    return round(statistics.quantiles(values, n=100)[int(p) - 1], 1)


async def produce(producer: AIOKafkaProducer, topic: str, n: int, rate: float | None) -> float:
    """Send n normal txns (timestamp = send time); return elapsed seconds. rate=None -> flat out."""
    factory = TransactionFactory(seed=1, suspicious_rate=0.0)
    loop = asyncio.get_running_loop()
    start = next_tick = loop.time()
    futures = []
    for _ in range(n):
        txn = factory.next_batch(datetime.now(UTC))[0][0]
        futures.append(
            await producer.send(topic, txn.model_dump_json().encode(), key=txn.customer_id.encode())
        )
        if rate:
            next_tick += 1 / rate
            await asyncio.sleep(max(0.0, next_tick - loop.time()))
    await asyncio.gather(*futures)  # wait for every broker ack
    return loop.time() - start


async def run_ingestor(
    bootstrap: str, topic: str, coll: Any, producer: AIOKafkaProducer, stop: asyncio.Event
) -> None:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=f"bench-{RUN}-{topic}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    try:
        await pipeline.Ingestor(consumer, producer, coll).run(stop)
    finally:
        await consumer.stop()


async def wait_for(coll: Any, n: int, limit_s: float = 600) -> float:
    start = time.perf_counter()
    while await coll.estimated_document_count() < n:
        if time.perf_counter() - start > limit_s:
            raise TimeoutError(f"only {await coll.estimated_document_count()} of {n} docs")
        await asyncio.sleep(0.05)
    return time.perf_counter() - start


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", default="localhost:9092")
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--lat-rate", type=float, default=500.0, help="msgs/sec for the latency run")
    ap.add_argument("--lat-seconds", type=float, default=20.0)
    args = ap.parse_args()

    admin = AIOKafkaAdminClient(bootstrap_servers=args.bootstrap)
    await admin.start()
    await admin.create_topics([NewTopic(t, PARTITIONS, 1) for t in TOPICS])
    await asyncio.sleep(3)  # partition leaders of brand-new topics; avoids NotLeader retries in timing
    mongo: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(os.environ["MONGO_URI"])
    db = mongo[f"bench_ingest_{RUN}"]
    producer = AIOKafkaProducer(bootstrap_servers=args.bootstrap, acks="all", enable_idempotence=True)
    await producer.start()
    try:
        for c in ("drain", "lat"):
            await pipeline.ensure_indexes(db[c])  # same indexes as production: writes pay for them

        # 1. producer only
        produce_s = await produce(producer, RAW, args.n, rate=None)
        print(f"producer: {args.n} msgs in {produce_s:.2f}s = {args.n / produce_s:.0f} msg/s")

        # 2. drain the backlog through the real Ingestor
        stop = asyncio.Event()
        task = asyncio.create_task(run_ingestor(args.bootstrap, RAW, db["drain"], producer, stop))
        drain_s = await wait_for(db["drain"], args.n)
        stop.set()
        await task
        exact = await db["drain"].count_documents({})  # sanity check: not the metadata estimate
        assert exact == args.n, f"expected {args.n} docs, found {exact}"
        print(
            f"ingest drain: {args.n} docs in {drain_s:.2f}s = {args.n / drain_s:.0f} msg/s"
            " (includes consumer group join)"
        )

        # 3. latency at a fixed rate, Ingestor running concurrently, measured downstream
        n_lat = int(args.lat_rate * args.lat_seconds)
        pipeline.TOPIC_VALIDATED = LAT_VALIDATED  # keep the drain run's events out of this measurement
        downstream = AIOKafkaConsumer(
            LAT_VALIDATED,
            bootstrap_servers=args.bootstrap,
            group_id=f"bench-{RUN}-downstream",
            auto_offset_reset="earliest",
        )
        await downstream.start()
        stop = asyncio.Event()
        task = asyncio.create_task(run_ingestor(args.bootstrap, LAT, db["lat"], producer, stop))
        await asyncio.sleep(5)  # let both consumers join their groups before the clock starts
        sender = asyncio.create_task(produce(producer, LAT, n_lat, rate=args.lat_rate))
        lat: list[float] = []
        while len(lat) < n_lat:
            batches = await downstream.getmany(timeout_ms=1000)
            received = datetime.now(UTC)
            for records in batches.values():
                for r in records:
                    sent = datetime.fromisoformat(json.loads(r.value)["timestamp"])
                    lat.append((received - sent).total_seconds() * 1000)
        await sender
        stop.set()
        await task
        await downstream.stop()
        result = {
            "run_at": datetime.now(UTC).isoformat(),
            "n": args.n,
            "producer_msgs_per_s": round(args.n / produce_s),
            "ingest_drain_msgs_per_s": round(args.n / drain_s),
            "latency_run": {
                "rate_msgs_per_s": args.lat_rate,
                "messages": len(lat),
                "p50_ms": pct(lat, 50),
                "p95_ms": pct(lat, 95),
                "p99_ms": pct(lat, 99),
                "max_ms": round(max(lat), 1),
            },
            "setup": "single-node Kafka 3.9.1 (6 partitions), MongoDB 8.0.34, Docker Desktop 8 GB, "
            "Apple Silicon 10 CPU / 16 GB laptop; one Ingestor process; live stack running alongside",
        }
        print(json.dumps(result, indent=2))
    finally:
        await producer.stop()
        await admin.delete_topics(list(TOPICS))
        await admin.close()
        await mongo.drop_database(f"bench_ingest_{RUN}")
        mongo.close()

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "ingest_throughput.json").write_text(json.dumps(result, indent=2))
    lr = result["latency_run"]
    (RESULTS / "ingest_throughput.md").write_text(
        f"# Ingest throughput — {result['run_at']}\n\n"
        f"Command: `make bench-ingest`. Setup: {result['setup']}.\n\n"
        "| metric | value |\n|---|---|\n"
        f"| producer only (acks=all, idempotent) | {result['producer_msgs_per_s']} msg/s |\n"
        f"| ingest drain ({args.n} backlog -> Mongo + validated topic) | "
        f"{result['ingest_drain_msgs_per_s']} msg/s |\n"
        f"| end-to-end latency (send -> validated event received) @ {lr['rate_msgs_per_s']:.0f} msg/s, "
        f"{lr['messages']} msgs | "
        f"p50 {lr['p50_ms']} ms, p95 {lr['p95_ms']} ms, p99 {lr['p99_ms']} ms, max {lr['max_ms']} ms |\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
