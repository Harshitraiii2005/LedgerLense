from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from gateway.app import Settings, create_app, init_state
from gateway.auth import mint_token
from gateway.repo import GATEWAY_INDEXES
from gateway.stream import Broadcaster
from gateway.upstream import Upstream
from ingest.pipeline import INDEXES, to_document
from ledgerlens_common.models import Transaction

SECRET = "test-secret-at-least-32-bytes-long!!"
BASE = datetime(2026, 10, 1, tzinfo=UTC)
N_TXNS = 45


def make_docs() -> list[dict]:
    """45 txns over 3 customers. Every 3 share a timestamp (to exercise the cursor tie-break);
    every 4th is scored and flagged; every 5th carries the internal synthetic label."""
    docs = []
    for i in range(N_TXNS):
        txn = Transaction(
            txn_id=f"txn{i:05d}",
            customer_id=f"C00000{i % 3 + 1}",
            merchant=["Acme Ltd", "Globex"][i % 2],
            category="groceries",
            amount=Decimal(f"{i + 1}.10"),
            currency="USD",
            country="US",
            channel="online",
            timestamp=BASE + timedelta(minutes=i // 3),
        )
        doc = to_document(txn, {"x-synthetic-pattern": "amount_spike"} if i % 5 == 0 else {})
        if i % 4 == 0:
            doc["risk"] = {
                "score": 80,
                "flagged": True,
                "reasons": [{"code": "AMOUNT_ZSCORE", "detail": "z=4.2"}],
                "scored_at": BASE,
            }
        elif i % 4 == 1:
            doc["risk"] = {"score": 5, "flagged": False, "reasons": [], "scored_at": BASE}
        docs.append(doc)
    return docs


@pytest.fixture(scope="session")
def mongo_url():
    from testcontainers.community.mongodb import MongoDbContainer

    with MongoDbContainer("mongo:8.0.34") as mongo:
        yield mongo.get_connection_url()


@pytest.fixture(scope="session")
async def db(mongo_url):
    client = AsyncIOMotorClient(mongo_url, tz_aware=True)
    database = client["gateway_test"]
    await database.transactions.insert_many(make_docs())
    await database.transactions.create_indexes(INDEXES + GATEWAY_INDEXES)
    await database.audit_log.insert_many(
        [
            {
                "user": "alice",
                "role": "analyst",
                "question": "q",
                "pipeline": [],
                "allowed": True,
                "timestamp": BASE + timedelta(minutes=i),
            }
            for i in range(3)
        ]
    )
    yield database
    client.close()


class MockUpstream:
    """Programmable fake for the risk/agent services: set .handler per test; .calls records requests."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(200, json={})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.handler(request)

    def upstream(self, name: str) -> Upstream:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url=f"http://{name}")
        return Upstream(name, client, retries=2, backoff_s=0)


def build_app(db=None, risk: MockUpstream | None = None, agent: MockUpstream | None = None, **settings):
    app = create_app(Settings(jwt_secret=SECRET, **settings), use_lifespan=False)
    init_state(
        app,
        db=db,
        risk=(risk or MockUpstream()).upstream("risk"),
        agent=(agent or MockUpstream()).upstream("agent"),
        broadcaster=Broadcaster(queue_size=3),
    )
    return app


@pytest.fixture
def risk():
    return MockUpstream()


@pytest.fixture
def agent():
    return MockUpstream()


@pytest.fixture
async def client(db, risk, agent):
    app = build_app(db, risk, agent, dev_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gw") as c:
        yield c


@pytest.fixture
def auth():
    """auth("admin") -> Authorization header dict for a freshly minted token."""

    def make(role: str = "analyst", sub: str = "alice", **kw) -> dict[str, str]:
        return {"Authorization": f"Bearer {mint_token(SECRET, sub, role, **kw)}"}

    return make


@pytest.fixture
def app_factory():
    return build_app


@pytest.fixture
def expected_order():
    """All seeded txn ids in the API's sort order: timestamp desc, then _id desc."""
    docs = sorted(make_docs(), key=lambda d: (d["timestamp"], d["_id"]), reverse=True)
    return [d["_id"] for d in docs], docs
