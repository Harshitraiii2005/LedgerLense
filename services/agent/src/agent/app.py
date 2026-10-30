"""Agent HTTP service (port 8003). Called only by the gateway, which supplies user + role from the JWT."""

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from ledgerlens_common.logging import REQUEST_ID_HEADER, configure_logging, new_request_id, request_id_var
from ledgerlens_semantic.catalog import Term
from ledgerlens_semantic.index import search
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from agent.planner import AdkPlanner, CatalogPlanner, Planner
from agent.service import QueryResult, QueryService

log = logging.getLogger("agent")


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "ledgerlens"
    # "auto" = ADK if an LLM key is configured, otherwise the deterministic catalog planner.
    agent_planner: Literal["auto", "adk", "catalog"] = "auto"
    agent_model: str = "gemini-flash-latest"  # or e.g. claude-sonnet-4-5 (needs ANTHROPIC_API_KEY)
    agent_max_rows: int = 200
    agent_timeout_ms: int = 5000
    port: int = 8003
    log_level: str = "INFO"


class QueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    user: str
    role: Literal["analyst", "admin"]


def make_planner(s: Settings) -> Planner:
    key = "ANTHROPIC_API_KEY" if s.agent_model.startswith("claude") else "GOOGLE_API_KEY"
    if s.agent_planner == "adk" or (s.agent_planner == "auto" and os.environ.get(key)):
        return AdkPlanner(s.agent_model)
    return CatalogPlanner()


def retrieve(question: str, k: int) -> list[Term]:
    return [hit.term for hit in search(question, k)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s: Settings = app.state.settings
    mongo: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(s.mongo_uri, serverSelectionTimeoutMS=5000)
    planner = make_planner(s)
    retrieve("warm up", 1)  # load the embedding model + FAISS index before taking traffic
    app.state.mongo = mongo
    app.state.service = QueryService(mongo[s.mongo_db], retrieve, planner, s.agent_max_rows, s.agent_timeout_ms)
    log.info("agent started", extra={"fields": {"planner": type(planner).__name__, "model": s.agent_model}})
    yield
    mongo.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="LedgerLens agent", lifespan=lifespan)
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
        try:
            await app.state.mongo.admin.command("ping")
            return JSONResponse({"ready": True})
        except Exception as e:
            return JSONResponse({"ready": False, "mongo": type(e).__name__}, status_code=503)

    @app.post("/query")
    async def query(body: QueryIn) -> QueryResult:
        result: QueryResult = await app.state.service.ask(body.question, body.user, body.role)
        return result

    return app


def run() -> None:
    settings = Settings()
    configure_logging("agent", settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None)  # noqa: S104
