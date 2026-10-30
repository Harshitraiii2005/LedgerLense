"""Contract tests for /v1 and /v2 against a real MongoDB (testcontainers).

The exact key sets below ARE the contract: if a field is added, renamed or removed,
these fail, which forces a deliberate decision (and a version bump for breaking changes).
"""

import httpx
import pytest

pytestmark = pytest.mark.integration

V1_KEYS = {
    "txn_id",
    "customer_id",
    "merchant",
    "category",
    "amount",
    "currency",
    "country",
    "channel",
    "timestamp",
    "risk_score",
    "flagged",
    "reason_codes",
}
V2_KEYS = {
    "txn_id",
    "customer_id",
    "merchant",
    "category",
    "money",
    "country",
    "channel",
    "timestamp",
    "risk",
}
ERROR_KEYS = {"code", "message", "request_id"}


def assert_error(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == ERROR_KEYS
    assert body["code"] == code
    assert body["request_id"] == r.headers["x-request-id"]


# --- v1 ------------------------------------------------------------------------------------


async def test_v1_list_shape_and_offset_pagination(client, auth, expected_order):
    ids, _ = expected_order
    seen = []
    for page in (1, 2, 3):
        r = await client.get("/v1/transactions", params={"page": page, "page_size": 20}, headers=auth())
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"items", "page", "page_size", "total"}
        assert body["total"] == 45 and body["page"] == page
        for item in body["items"]:
            assert set(item) == V1_KEYS
            assert isinstance(item["amount"], float)
        seen += [i["txn_id"] for i in body["items"]]
    assert seen == ids  # newest first, no gaps or duplicates across pages


async def test_v1_flat_risk_fields(client, auth):
    r = await client.get("/v1/transactions/txn00004", headers=auth())
    item = r.json()
    assert item["risk_score"] == 80 and item["flagged"] is True
    assert item["reason_codes"] == ["AMOUNT_ZSCORE"]
    assert item["amount"] == 5.1
    unscored = (await client.get("/v1/transactions/txn00002", headers=auth())).json()
    assert unscored["risk_score"] is None and unscored["flagged"] is False and unscored["reason_codes"] == []


async def test_v1_filters(client, auth):
    r = await client.get("/v1/transactions", params={"flagged": "true", "page_size": 100}, headers=auth())
    items = r.json()["items"]
    assert len(items) == 12 and all(i["flagged"] for i in items)  # i % 4 == 0 for i < 45
    r = await client.get("/v1/transactions", params={"flagged": "false", "page_size": 100}, headers=auth())
    assert r.json()["total"] == 33  # includes never-scored txns
    r = await client.get(
        "/v1/transactions", params={"customer_id": "C000002", "merchant": "Globex"}, headers=auth()
    )
    assert all(i["customer_id"] == "C000002" and i["merchant"] == "Globex" for i in r.json()["items"])
    r = await client.get(
        "/v1/transactions",
        params={"from": "2026-10-01T00:10:00Z", "to": "2026-10-01T00:12:00Z"},
        headers=auth(),
    )
    assert r.json()["total"] == 6  # minutes 10 and 11, three txns each


async def test_internal_fields_never_exposed(client, auth):
    for path in ("/v1/transactions/txn00000", "/v2/transactions/txn00000"):
        body = (await client.get(path, headers=auth())).json()
        assert "synthetic_pattern" not in body and "_id" not in body and "ingested_at" not in body


async def test_v1_not_found_and_validation_errors(client, auth):
    assert_error(await client.get("/v1/transactions/nope", headers=auth()), 404, "not_found")
    assert_error(await client.get("/v1/transactions?page_size=0", headers=auth()), 422, "validation_error")
    assert_error(
        await client.get("/v1/transactions?customer_id=bob", headers=auth()), 422, "validation_error"
    )
    assert_error(await client.get("/no/such/route", headers=auth()), 404, "not_found")


# --- v2 ------------------------------------------------------------------------------------


async def test_v2_shape(client, auth):
    body = (await client.get("/v2/transactions", params={"limit": 5}, headers=auth())).json()
    assert set(body) == {"data", "next_cursor"}
    assert len(body["data"]) == 5
    for item in body["data"]:
        assert set(item) == V2_KEYS
        assert set(item["money"]) == {"amount", "currency"}
        assert isinstance(item["money"]["amount"], str)
    flagged = (await client.get("/v2/transactions/txn00004", headers=auth())).json()
    assert flagged["money"] == {"amount": "5.10", "currency": "USD"}  # exact decimal, trailing zero kept
    assert set(flagged["risk"]) == {"score", "flagged", "reasons", "scored_at"}
    assert flagged["risk"]["reasons"] == [{"code": "AMOUNT_ZSCORE", "detail": "z=4.2"}]
    assert (await client.get("/v2/transactions/txn00002", headers=auth())).json()["risk"] is None


@pytest.mark.parametrize("limit", [1, 7, 45, 100])
async def test_v2_cursor_walk_visits_every_row_once(client, auth, expected_order, limit):
    ids, _ = expected_order
    seen, cursor = [], None
    while True:
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        body = (await client.get("/v2/transactions", params=params, headers=auth())).json()
        seen += [i["txn_id"] for i in body["data"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ids  # timestamp ties are broken by _id, so nothing is skipped or repeated


async def test_v2_cursor_with_filter(client, auth):
    seen, cursor = [], None
    while True:
        params = {"limit": 5, "flagged": "true"} | ({"cursor": cursor} if cursor else {})
        body = (await client.get("/v2/transactions", params=params, headers=auth())).json()
        assert all(i["risk"]["flagged"] for i in body["data"])
        seen += body["data"]
        if not (cursor := body["next_cursor"]):
            break
    assert len(seen) == 12


async def test_v2_invalid_cursor(client, auth):
    assert_error(await client.get("/v2/transactions?cursor=%%%", headers=auth()), 400, "invalid_cursor")
    assert_error(
        await client.get("/v2/transactions?cursor=bm90IGpzb24=", headers=auth()), 400, "invalid_cursor"
    )


async def test_v1_still_works_alongside_v2(client, auth):
    v1 = (await client.get("/v1/transactions/txn00008", headers=auth())).json()
    v2 = (await client.get("/v2/transactions/txn00008", headers=auth())).json()
    assert v1["amount"] == float(v2["money"]["amount"])
    assert v1["risk_score"] == v2["risk"]["score"]


# --- v2 composite / proxied endpoints ----------------------------------------------------------


async def test_customer_detail_merges_mongo_and_risk(client, auth, risk):
    risk.handler = lambda req: httpx.Response(200, json={"customer_id": "C000001", "risk_level": "high"})
    r = await client.get("/v2/customers/C000001", headers=auth() | {"X-Request-ID": "rid-123"})
    body = r.json()
    assert r.status_code == 200
    assert body["risk_profile"] == {"customer_id": "C000001", "risk_level": "high"}
    assert body["risk_profile_error"] is None
    assert len(body["recent_transactions"]) == 15
    assert all(t["customer_id"] == "C000001" for t in body["recent_transactions"])
    assert risk.calls[0].url.path == "/customers/C000001/risk"
    assert risk.calls[0].headers["x-request-id"] == "rid-123"  # propagated downstream


async def test_customer_detail_degrades_when_risk_is_down(client, auth, risk):
    def down(req):
        raise httpx.ConnectError("refused")

    risk.handler = down
    body = (await client.get("/v2/customers/C000001", headers=auth())).json()
    assert body["risk_profile"] is None
    assert body["risk_profile_error"]["code"] == "upstream_unavailable"
    assert len(body["recent_transactions"]) == 15
    assert len(risk.calls) == 3  # 1 try + 2 retries (GET is idempotent)


async def test_unknown_customer_is_404(client, auth, risk):
    risk.handler = lambda req: httpx.Response(
        404, json={"code": "customer_not_found", "message": "no profile"}
    )
    assert_error(await client.get("/v2/customers/C999999", headers=auth()), 404, "customer_not_found")
    assert len(risk.calls) == 1  # 4xx is never retried


async def test_top_risky_proxy(client, auth, risk):
    risk.handler = lambda req: httpx.Response(200, json={"data": [{"customer_id": "C000001", "score": 91}]})
    r = await client.get("/v2/reports/top-risky", params={"limit": 5}, headers=auth())
    assert r.json() == {"data": [{"customer_id": "C000001", "score": 91}]}
    assert risk.calls[0].url.params["limit"] == "5"


async def test_agent_query_identity_comes_from_jwt(client, auth, agent):
    agent.handler = lambda req: httpx.Response(
        200, json={"answer": "42", "terms_used": [], "pipeline": [], "rows": [], "allowed": True}
    )
    r = await client.post(
        "/v2/agent/query",
        json={"question": "how many flagged?", "user": "mallory"},
        headers=auth("admin", sub="bob"),
    )
    assert r.status_code == 200 and r.json()["answer"] == "42"
    import json

    assert json.loads(agent.calls[0].content) == {
        "question": "how many flagged?",
        "user": "bob",
        "role": "admin",
    }


async def test_agent_post_not_retried_on_server_error(client, auth, agent):
    agent.handler = lambda req: httpx.Response(500)
    assert_error(
        await client.post("/v2/agent/query", json={"question": "hello?"}, headers=auth()),
        502,
        "upstream_error",
    )
    assert len(agent.calls) == 1  # the POST may have executed; a retry could double-run it


async def test_agent_unavailable_is_503(client, auth, agent):
    def down(req):
        raise httpx.ConnectError("refused")

    agent.handler = down
    assert_error(
        await client.post("/v2/agent/query", json={"question": "hello?"}, headers=auth()),
        503,
        "upstream_unavailable",
    )
    assert len(agent.calls) == 3  # connect failures are safe to retry even for POST


# --- RBAC ------------------------------------------------------------------------------------


async def test_rbac(client, auth):
    from datetime import timedelta

    assert_error(await client.get("/v1/transactions"), 401, "unauthorized")
    assert_error(
        await client.get("/v2/transactions", headers={"Authorization": "Bearer junk"}), 401, "unauthorized"
    )
    expired = auth(ttl=timedelta(seconds=-1))
    assert_error(await client.get("/v2/transactions", headers=expired), 401, "token_expired")
    assert_error(await client.get("/v2/audit", headers=auth("analyst")), 403, "forbidden")

    r = await client.get("/v2/audit", headers=auth("admin"))
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 3 and data[0]["timestamp"] > data[-1]["timestamp"]
    assert isinstance(data[0]["_id"], str)


async def test_token_signed_with_other_secret_rejected(client):
    from gateway.auth import mint_token

    forged = mint_token("some-other-secret-that-is-long-enough", "eve", "admin")
    assert_error(
        await client.get("/v2/audit", headers={"Authorization": f"Bearer {forged}"}), 401, "unauthorized"
    )


async def test_dev_token_endpoint(client):
    r = await client.post("/dev/token", json={"sub": "dana", "role": "admin"})
    token = r.json()["access_token"]
    assert (await client.get("/v2/audit", headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_health(client):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    r = await client.get("/health/ready")  # no Kafka consumer attached in tests
    assert r.status_code == 503
    assert r.json()["checks"]["mongo"] == "ok"
