"""/v1 — original contract: offset pagination, flat risk fields, amount as a JSON number.

Frozen: existing clients depend on it. New features go to /v2 (see docs/api-versioning.md).
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from gateway.auth import AnyUser
from gateway.errors import ApiError
from gateway.repo import Doc, TxnFilter, find_page, txn_filter

router = APIRouter(prefix="/v1", tags=["v1"])


class TxnV1(BaseModel):
    txn_id: str
    customer_id: str
    merchant: str
    category: str
    amount: float  # legacy: a float. v2 fixes this with an exact decimal string.
    currency: str
    country: str
    channel: str
    timestamp: datetime
    risk_score: int | None
    flagged: bool
    reason_codes: list[str]


class PageV1(BaseModel):
    items: list[TxnV1]
    page: int
    page_size: int
    total: int


def to_v1(d: Doc) -> TxnV1:
    risk = d.get("risk") or {}
    return TxnV1(
        txn_id=d["_id"],
        customer_id=d["customer_id"],
        merchant=d["merchant"],
        category=d["category"],
        amount=float(d["amount"].to_decimal()),
        currency=d["currency"],
        country=d["country"],
        channel=d["channel"],
        timestamp=d["timestamp"],
        risk_score=risk.get("score"),
        flagged=bool(risk.get("flagged", False)),
        reason_codes=[r["code"] for r in risk.get("reasons", [])],
    )


@router.get("/transactions")
async def list_transactions(
    request: Request,
    _: AnyUser,
    f: Annotated[TxnFilter, Depends(txn_filter)],
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> PageV1:
    docs, total = await find_page(request.app.state.db.transactions, f, page, page_size)
    return PageV1(items=[to_v1(d) for d in docs], page=page, page_size=page_size, total=total)


@router.get("/transactions/{txn_id}")
async def get_transaction(request: Request, _: AnyUser, txn_id: str) -> TxnV1:
    doc = await request.app.state.db.transactions.find_one({"_id": txn_id})
    if doc is None:
        raise ApiError(404, "not_found", f"transaction {txn_id} not found")
    return to_v1(doc)
