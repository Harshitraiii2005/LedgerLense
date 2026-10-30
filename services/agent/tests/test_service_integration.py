"""QueryService against a real MongoDB: rejected plans never execute, every query is audited."""

from collections.abc import Iterator

import pytest
from ledgerlens_semantic.catalog import default_catalog
from motor.motor_asyncio import AsyncIOMotorClient
from testcontainers.community.mongodb import MongoDbContainer

from agent.planner import Plan
from agent.service import QueryService

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def mongo_url() -> Iterator[str]:
    with MongoDbContainer("mongo:8.0.34") as mongo:
        yield mongo.get_connection_url()


class FixedPlanner:
    """Stands in for the LLM: returns whatever plan the test wants, including malicious ones."""

    def __init__(self, collection, pipeline):
        self.plan_ = Plan(
            collection=collection, pipeline=pipeline, terms_used=["flagged_transaction"], planner="fixed"
        )

    async def plan(self, question, terms, role):
        return self.plan_


def retrieve(question, k):
    return [default_catalog().get("flagged_transaction")]


async def test_malicious_plan_is_rejected_and_audited(mongo_url):
    db = AsyncIOMotorClient(mongo_url)["agent_it"]
    await db.transactions.insert_one({"_id": "t1", "customer_id": "C000001", "risk": {"flagged": True}})
    svc = QueryService(db, retrieve, FixedPlanner("transactions", [{"$match": {}}, {"$out": "stolen"}]))

    res = await svc.ask("copy everything", user="mallory", role="analyst")

    assert not res.allowed and "$out" in (res.reason or "")
    assert res.rows == []
    assert "stolen" not in await db.list_collection_names()  # never executed
    audit = await db.audit_log.find_one({"user": "mallory"})
    assert audit["allowed"] is False and audit["pipeline"][1] == {"$out": "stolen"}


async def test_allowed_query_masks_pii_and_audits(mongo_url):
    db = AsyncIOMotorClient(mongo_url)["agent_it2"]
    await db.transactions.insert_many([{"_id": f"t{i}", "customer_id": f"C00000{i}"} for i in range(3)])
    svc = QueryService(db, retrieve, FixedPlanner("transactions", [{"$project": {"customer_id": 1}}]))

    res = await svc.ask("list customers", user="ana", role="analyst")

    assert res.allowed
    assert [r["customer_id"] for r in res.rows] == ["C0***00", "C0***01", "C0***02"]
    assert res.pipeline[-1] == {"$limit": 200}  # row cap appended by the guard
    assert await db.audit_log.count_documents({"user": "ana", "allowed": True}) == 1
