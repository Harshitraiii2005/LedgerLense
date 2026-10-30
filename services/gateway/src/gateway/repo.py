"""MongoDB reads for the transaction endpoints, shared by /v1 and /v2.

Both versions use the same filters and the same sort (newest first, _id as tie-break);
they differ only in pagination (offset vs cursor) and response shape.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import DESCENDING, IndexModel

from gateway.errors import ApiError

Doc = dict[str, Any]
Collection = AsyncIOMotorCollection[Doc]

SORT = [("timestamp", DESCENDING), ("_id", DESCENDING)]
# Serves the unfiltered newest-first feed and makes the (timestamp, _id) cursor an index walk.
# The filtered queries use ingest's customer_ts / merchant_ts and risk's flagged_ts.
GATEWAY_INDEXES = [IndexModel(SORT, name="ts_id")]


@dataclass(frozen=True)
class TxnFilter:
    customer_id: str | None = None
    merchant: str | None = None
    flagged: bool | None = None
    since: datetime | None = None
    until: datetime | None = None

    def to_mongo(self) -> Doc:
        q: Doc = {}
        if self.customer_id:
            q["customer_id"] = self.customer_id
        if self.merchant:
            q["merchant"] = self.merchant
        if self.flagged is not None:
            # Not-yet-scored transactions have no `risk` field; they count as not flagged.
            q["risk.flagged"] = True if self.flagged else {"$ne": True}
        if self.since or self.until:
            q["timestamp"] = {k: v for k, v in (("$gte", self.since), ("$lt", self.until)) if v}
        return q


def txn_filter(
    customer_id: Annotated[str | None, Query(pattern=r"^C\d{6}$")] = None,
    merchant: Annotated[str | None, Query(max_length=120)] = None,
    flagged: bool | None = None,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
) -> TxnFilter:
    """FastAPI dependency: the query-string filters both API versions accept."""
    return TxnFilter(customer_id, merchant, flagged, since, until)


# --- v1: offset pagination ---------------------------------------------------------------


async def find_page(coll: Collection, f: TxnFilter, page: int, page_size: int) -> tuple[list[Doc], int]:
    # Trade-off: skip() + count_documents() scan everything before the page, O(offset).
    # That cost is exactly why v2 moved to cursors; v1 keeps it for compatibility.
    q = f.to_mongo()
    docs = await coll.find(q).sort(SORT).skip((page - 1) * page_size).limit(page_size).to_list(page_size)
    return docs, await coll.count_documents(q)


# --- v2: cursor (keyset) pagination ------------------------------------------------------


def encode_cursor(doc: Doc) -> str:
    raw = json.dumps([doc["timestamp"].isoformat(), doc["_id"]])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        ts, txn_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(ts), str(txn_id)
    except (binascii.Error, ValueError, TypeError):
        raise ApiError(400, "invalid_cursor", "cursor is malformed") from None


async def find_after(
    coll: Collection, f: TxnFilter, cursor: str | None, limit: int
) -> tuple[list[Doc], str | None]:
    q = f.to_mongo()
    if cursor:
        ts, txn_id = decode_cursor(cursor)
        # "Strictly after the last item seen" in (timestamp desc, _id desc) order.
        after = {"$or": [{"timestamp": {"$lt": ts}}, {"timestamp": ts, "_id": {"$lt": txn_id}}]}
        q = {"$and": [q, after]} if q else after
    # Fetch one extra row: if it exists there is a next page, without a separate count query.
    docs = await coll.find(q).sort(SORT).limit(limit + 1).to_list(limit + 1)
    page = docs[:limit]
    return page, encode_cursor(page[-1]) if len(docs) > limit else None
