"""Ingest service: FastAPI app whose lifespan owns the Kafka consumer task."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from ingest.pipeline import Ingestor, ensure_indexes
from ledgerlens_common import TOPIC_RAW
from ledgerlens_common.logging import REQUEST_ID_HEADER, configure_logging, new_request_id, request_id_var
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic_settings import BaseSettings

log = logging.getLogger("ingest")


class Settings(BaseSettings):
    kafka_bootstrap: str = "localhost:9092"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "ledgerlens"
    consumer_group: str = "ingest"
    port: int = 8000
    log_level: str = "INFO"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s: Settings = app.state.settings
    mongo: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(s.mongo_uri, serverSelectionTimeoutMS=5000)
    collection = mongo[s.mongo_db]["transactions"]
    await ensure_indexes(collection)

    producer = AIOKafkaProducer(bootstrap_servers=s.kafka_bootstrap, acks="all", enable_idempotence=True)
    consumer = AIOKafkaConsumer(
        TOPIC_RAW,
        bootstrap_servers=s.kafka_bootstrap,
        group_id=s.consumer_group,
        enable_auto_commit=False,  # we commit only after Mongo + downstream publish succeed
        auto_offset_reset="earliest",
    )
    await producer.start()
    await consumer.start()

    ingestor = Ingestor(consumer, producer, collection)
    stop = asyncio.Event()
    task = asyncio.create_task(ingestor.run(stop), name="ingest-consumer")
    app.state.mongo, app.state.consumer, app.state.task, app.state.ingestor = mongo, consumer, task, ingestor
    log.info("ingest started")
    try:
        yield
    finally:
        # Graceful shutdown: let the in-flight batch finish and commit, then close clients.
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=15)
        except TimeoutError:
            task.cancel()
        await consumer.stop()
        await producer.stop()
        mongo.close()
        log.info("ingest stopped", extra={"fields": ingestor.stats})


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="LedgerLens ingest", lifespan=lifespan)
    app.state.settings = settings or Settings()

    @app.middleware("http")
    async def request_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = rid
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            await asyncio.wait_for(app.state.mongo.admin.command("ping"), timeout=2)
            checks["mongo"] = "ok"
        except Exception as e:
            checks["mongo"] = f"error: {type(e).__name__}"
        try:
            await asyncio.wait_for(app.state.consumer.topics(), timeout=2)  # metadata round trip
            checks["kafka"] = "ok"
        except Exception as e:
            checks["kafka"] = f"error: {type(e).__name__}"
        checks["consumer"] = "stopped" if app.state.task.done() else "ok"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": healthy, "checks": checks, "stats": app.state.ingestor.stats},
                            status_code=200 if healthy else 503)  # fmt: skip

    return app


def run() -> None:
    settings = Settings()
    configure_logging("ingest", settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None)  # noqa: S104
