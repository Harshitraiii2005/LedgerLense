"""One analyst question end to end: retrieve -> plan -> validate -> execute -> mask -> audit."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from bson import Decimal128, ObjectId
from ledgerlens_semantic.catalog import Term
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from pymongo.errors import ExecutionTimeout, OperationFailure

from agent.guard import PipelineRejected, validate
from agent.pii import mask
from agent.planner import Planner

log = logging.getLogger("agent.service")
Retriever = Callable[[str, int], list[Term]]


class QueryResult(BaseModel):
    answer: str
    terms_used: list[str]
    pipeline: Any
    rows: list[dict[str, Any]]
    allowed: bool
    planner: str
    reason: str | None = None


def to_json(value: Any) -> Any:
    """BSON values -> plain JSON types (Decimal128 -> string keeps money exact)."""
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_json(v) for v in value]
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, Decimal | ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def compose_answer(rows: list[dict[str, Any]], terms: list[Term]) -> str:
    """Deterministic answer text: the numbers come from the rows, never from the model."""
    cited = "; ".join(f"{t.name} = {t.formula}" for t in terms)
    if not rows:
        body = "No matching data."
    elif len(rows) == 1:
        body = ", ".join(f"{k}: {v}" for k, v in rows[0].items() if k != "_id" or v is not None)
    else:
        body = f"{len(rows)} rows returned; first: " + ", ".join(f"{k}: {v}" for k, v in rows[0].items())
    return f"{body} (definitions used: {cited})"


class QueryService:
    def __init__(
        self,
        db: AsyncIOMotorDatabase[dict[str, Any]],
        retrieve: Retriever,
        planner: Planner,
        max_rows: int = 200,
        timeout_ms: int = 5000,
    ) -> None:
        self.db, self.retrieve, self.planner = db, retrieve, planner
        self.max_rows, self.timeout_ms = max_rows, timeout_ms

    async def ask(self, question: str, user: str, role: str) -> QueryResult:
        terms = self.retrieve(question, 4)
        plan = await self.planner.plan(question, terms, role)
        cited = [t for t in terms if t.name in plan.terms_used] or terms[:1]
        try:
            safe = validate(plan.collection, plan.pipeline, role, self.max_rows)
            cursor = self.db[plan.collection].aggregate(safe, maxTimeMS=self.timeout_ms)
            rows = mask(to_json(await cursor.to_list(self.max_rows)))
        except PipelineRejected as e:
            return await self._finish(question, user, role, plan.pipeline, plan, cited, [], str(e))
        except (ExecutionTimeout, OperationFailure) as e:
            # Treated like a rejection: the pipeline is recorded, nothing is returned.
            return await self._finish(question, user, role, plan.pipeline, plan, cited, [], f"execution: {e}")
        return await self._finish(question, user, role, safe, plan, cited, rows, None)

    async def _finish(
        self,
        question: str,
        user: str,
        role: str,
        pipeline: Any,
        plan: Any,
        cited: list[Term],
        rows: list[dict[str, Any]],
        reason: str | None,
    ) -> QueryResult:
        allowed = reason is None
        # Every query is audited, allowed or not, before the caller sees a response.
        await self.db.audit_log.insert_one({
            "user": user, "role": role, "question": question, "pipeline": pipeline,
            "collection": plan.collection, "terms_used": [t.name for t in cited],
            "planner": plan.planner, "allowed": allowed, "reason": reason,
            "row_count": len(rows), "timestamp": datetime.now(UTC),
        })  # fmt: skip
        log.info("query", extra={"fields": {"allowed": allowed, "reason": reason, "planner": plan.planner}})
        answer = compose_answer(rows, cited) if allowed else f"Query rejected: {reason}"
        return QueryResult(answer=answer, terms_used=[t.name for t in cited], pipeline=pipeline, rows=rows,
                           allowed=allowed, planner=plan.planner, reason=reason)  # fmt: skip
