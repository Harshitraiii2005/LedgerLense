"""Gateway service: FastAPI app wiring (settings, lifespan, middleware, health, routers)."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import uvicorn
from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from gateway import v1, v2
from gateway.auth import Role, mint_token
from gateway.errors import install_error_handlers
from gateway.repo import GATEWAY_INDEXES
from gateway.stream import Broadcaster, consume_flagged, make_consumer
from gateway.upstream import Upstream
from ledgerlens_common import TOPIC_FLAGGED
from ledgerlens_common.logging import REQUEST_ID_HEADER, configure_logging, new_request_id, request_id_var

log = logging.getLogger("gateway")


class Settings(BaseSettings):
    jwt_secret: str  # required: no default, so a missing Secret fails fast at startup
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "ledgerlens"
    kafka_bootstrap: str = "localhost:9092"
    risk_url: str = "http://localhost:8001"
    agent_url: str = "http://localhost:8003"
    upstream_timeout_s: float = 3.0
    agent_timeout_s: float = 30.0  # LLM calls are slow
    upstream_retries: int = 3
    ws_queue_size: int = 100
    dev_mode: bool = False  # enables POST /dev/token; never set in a real deployment
    port: int = 8002
    log_level: str = "INFO"


def init_state(
    app: FastAPI,
    *,
    db: AsyncIOMotorDatabase[dict[str, Any]],
    risk: Upstream,
    agent: Upstream,
    broadcaster: Broadcaster,
    consumer: AIOKafkaConsumer | None = None,
) -> None:
    """Everything route handlers use lives on app.state; tests call this with their own fakes."""
    app.state.db, app.state.risk, app.state.agent = db, risk, agent
    app.state.broadcaster, app.state.consumer = broadcaster, consumer


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s: Settings = app.state.settings
    mongo: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(
        s.mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000
    )
    db = mongo[s.mongo_db]
    await db.transactions.create_indexes(GATEWAY_INDEXES)
    risk_http = httpx.AsyncClient(
        base_url=s.risk_url, timeout=httpx.Timeout(s.upstream_timeout_s, connect=1.0)
    )
    agent_http = httpx.AsyncClient(
        base_url=s.agent_url, timeout=httpx.Timeout(s.agent_timeout_s, connect=1.0)
    )
    broadcaster = Broadcaster(s.ws_queue_size)
    consumer = make_consumer(s.kafka_bootstrap, TOPIC_FLAGGED)
    await consumer.start()
    task = asyncio.create_task(consume_flagged(consumer, broadcaster), name="flagged-consumer")
    init_state(
        app,
        db=db,
        risk=Upstream("risk", risk_http, s.upstream_retries),
        agent=Upstream("agent", agent_http, s.upstream_retries),
        broadcaster=broadcaster,
        consumer=consumer,
    )
    log.info("gateway started")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await consumer.stop()
        await risk_http.aclose()
        await agent_http.aclose()
        mongo.close()


class TokenRequest(BaseModel):
    sub: str
    role: Role


def create_app(settings: Settings | None = None, *, use_lifespan: bool = True) -> FastAPI:
    app = FastAPI(title="LedgerLens gateway", lifespan=lifespan if use_lifespan else None)
    app.state.settings = settings = settings or Settings()  # fields not passed are read from env
    install_error_handlers(app)
    app.include_router(v1.router)
    app.include_router(v2.router)

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
            await asyncio.wait_for(app.state.db.command("ping"), timeout=2)
            checks["mongo"] = "ok"
        except Exception as e:
            checks["mongo"] = f"error: {type(e).__name__}"
        try:
            await asyncio.wait_for(app.state.consumer.topics(), timeout=2)  # metadata round trip
            checks["kafka"] = "ok"
        except Exception as e:
            checks["kafka"] = f"error: {type(e).__name__}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": healthy, "checks": checks}, status_code=200 if healthy else 503)

    if settings.dev_mode:
        # Local/demo login for the dashboard. Registered only in dev mode, so it doesn't exist in prod.
        @app.post("/dev/token")
        async def dev_token(body: TokenRequest) -> dict[str, str]:
            return {
                "access_token": mint_token(settings.jwt_secret, body.sub, body.role),
                "token_type": "bearer",
            }

    return app


def run() -> None:
    settings = Settings()
    configure_logging("gateway", settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None)  # noqa: S104
