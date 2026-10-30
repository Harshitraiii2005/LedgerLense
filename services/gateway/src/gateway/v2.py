"""/v2: cursor pagination, nested `risk` object, exact money; plus customer, report, agent,
audit and live-stream endpoints. Breaking changes from v1 are listed in docs/api-versioning.md.
"""

import asyncio
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from gateway.auth import Admin, AnyUser, decode_token
from gateway.errors import ApiError
from gateway.repo import SORT, Doc, TxnFilter, find_after, txn_filter
from ledgerlens_common.models import RiskReason

router = APIRouter(prefix="/v2", tags=["v2"])


class Money(BaseModel):
    amount: str  # exact decimal as a string ("12.30"); JSON numbers are floats in most clients
    currency: str


class RiskV2(BaseModel):
    score: int
    flagged: bool
    reasons: list[RiskReason]
    scored_at: datetime


class TxnV2(BaseModel):
    txn_id: str
    customer_id: str
    merchant: str
    category: str
    money: Money
    country: str
    channel: str
    timestamp: datetime
    risk: RiskV2 | None  # null until the risk service has scored the transaction


class PageV2(BaseModel):
    data: list[TxnV2]
    next_cursor: str | None  # pass back as ?cursor= ; null on the last page


class ErrorBody(BaseModel):
    code: str
    message: str


class CustomerV2(BaseModel):
    customer_id: str
    recent_transactions: list[TxnV2]
    risk_profile: dict[str, Any] | None
    # Degraded mode: if the risk service is down, the page still renders with Mongo data.
    risk_profile_error: ErrorBody | None = None


class AgentQuery(BaseModel):
    question: str = Field(min_length=3, max_length=500)


def to_v2(d: Doc) -> TxnV2:
    risk = d.get("risk")
    return TxnV2(
        txn_id=d["_id"],
        customer_id=d["customer_id"],
        merchant=d["merchant"],
        category=d["category"],
        money=Money(amount=str(d["amount"].to_decimal()), currency=d["currency"]),
        country=d["country"],
        channel=d["channel"],
        timestamp=d["timestamp"],
        risk=RiskV2.model_validate(risk) if risk else None,
    )


@router.get("/transactions")
async def list_transactions(
    request: Request,
    _: AnyUser,
    f: Annotated[TxnFilter, Depends(txn_filter)],
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageV2:
    docs, next_cursor = await find_after(request.app.state.db.transactions, f, cursor, limit)
    return PageV2(data=[to_v2(d) for d in docs], next_cursor=next_cursor)


@router.get("/transactions/{txn_id}")
async def get_transaction(request: Request, _: AnyUser, txn_id: str) -> TxnV2:
    doc = await request.app.state.db.transactions.find_one({"_id": txn_id})
    if doc is None:
        raise ApiError(404, "not_found", f"transaction {txn_id} not found")
    return to_v2(doc)


@router.get("/customers/{customer_id}")
async def get_customer(request: Request, _: AnyUser, customer_id: str) -> CustomerV2:
    state = request.app.state

    async def profile_or_error() -> dict[str, Any] | ApiError:
        try:
            profile: dict[str, Any] = await state.risk.request("GET", f"/customers/{customer_id}/risk")
            return profile
        except ApiError as e:
            return e

    # Mongo and the risk service are independent, so query them concurrently.
    docs: list[Doc]
    docs, profile = await asyncio.gather(
        state.db.transactions.find({"customer_id": customer_id}).sort(SORT).to_list(20), profile_or_error()
    )
    error = None
    if isinstance(profile, ApiError):
        if not docs:
            raise profile  # nothing to show: unknown customer (404) or risk service down (5xx)
        error, profile = ErrorBody(code=profile.code, message=profile.message), None
    return CustomerV2(
        customer_id=customer_id,
        recent_transactions=[to_v2(d) for d in docs],
        risk_profile=profile,
        risk_profile_error=error,
    )


@router.get("/reports/top-risky")
async def top_risky(request: Request, _: AnyUser, limit: Annotated[int, Query(ge=1, le=100)] = 10) -> Any:
    return await request.app.state.risk.request("GET", "/reports/top-risky", params={"limit": limit})


@router.post("/agent/query")
async def agent_query(request: Request, user: AnyUser, body: AgentQuery) -> Any:
    # Identity comes from the verified JWT, never from the request body, so the
    # agent's audit log can't be spoofed by the caller.
    payload = {"question": body.question, "user": user.sub, "role": user.role}
    return await request.app.state.agent.request("POST", "/query", json=payload)


@router.get("/audit")
async def audit_log(
    request: Request,
    _: Admin,
    user: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, list[dict[str, Any]]]:
    q = {"user": user} if user else {}
    docs = await request.app.state.db.audit_log.find(q).sort("timestamp", -1).to_list(limit)
    return {"data": [d | {"_id": str(d["_id"])} for d in docs]}


@router.websocket("/ws/flagged")
async def ws_flagged(ws: WebSocket, token: str = "") -> None:
    # Browsers can't set headers on a WebSocket handshake, so the JWT comes as ?token=.
    try:
        decode_token(ws.app.state.settings.jwt_secret, token)
    except ApiError:
        await ws.close(code=1008)  # policy violation; before accept() this rejects the handshake
        return
    await ws.accept()
    broadcaster = ws.app.state.broadcaster
    q = broadcaster.subscribe()

    async def pump() -> None:
        while True:
            await ws.send_text(await q.get())

    sender = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # we ignore client messages; this raises on disconnect
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        broadcaster.unsubscribe(q)
