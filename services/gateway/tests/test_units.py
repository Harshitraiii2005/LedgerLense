"""Docker-free tests: upstream retry policy, broadcaster, Kafka fan-out, WebSocket auth + streaming."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.errors import ApiError
from gateway.stream import Broadcaster, consume_flagged
from ledgerlens_common.models import RiskAssessment, Transaction


async def test_get_retries_5xx_then_succeeds(risk):
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": True})])
    risk.handler = lambda req: next(responses)
    assert await risk.upstream("risk").request("GET", "/x") == {"ok": True}
    assert len(risk.calls) == 3


async def test_get_timeout_gives_504_after_retries(risk):
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    risk.handler = slow
    with pytest.raises(ApiError) as e:
        await risk.upstream("risk").request("GET", "/x")
    assert (e.value.status, e.value.code) == (504, "upstream_timeout")
    assert len(risk.calls) == 3


async def test_post_timeout_is_not_retried(risk):
    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    risk.handler = slow
    with pytest.raises(ApiError):
        await risk.upstream("risk").request("POST", "/score", json={})
    assert len(risk.calls) == 1


async def test_upstream_4xx_with_non_json_body(risk):
    risk.handler = lambda req: httpx.Response(400, text="bad")
    with pytest.raises(ApiError) as e:
        await risk.upstream("risk").request("GET", "/x")
    assert (e.value.status, e.value.code) == (400, "upstream_rejected")


def test_broadcaster_drops_oldest_for_slow_clients():
    b = Broadcaster(queue_size=2)
    q = b.subscribe()
    for m in "abc":
        b.publish(m)
    assert [q.get_nowait(), q.get_nowait()] == ["b", "c"]
    assert b.dropped == 1
    b.unsubscribe(q)
    b.publish("d")
    assert q.empty()


def assessment(flagged: bool) -> bytes:
    txn = Transaction(
        txn_id="txn00001",
        customer_id="C000001",
        merchant="Acme",
        category="travel",
        amount="10.00",
        currency="USD",
        country="US",
        channel="online",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    return (
        RiskAssessment(
            txn_id=txn.txn_id,
            customer_id=txn.customer_id,
            score=90 if flagged else 1,
            flagged=flagged,
            reasons=[],
            scored_at=txn.timestamp,
            transaction=txn,
        )
        .model_dump_json()
        .encode()
    )


@dataclass
class Msg:
    value: bytes
    offset: int = 0


class FakeConsumer:
    def __init__(self, values):
        self.values = values

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for v in self.values:
            yield Msg(v)


async def test_consume_forwards_only_valid_flagged_events():
    b = Broadcaster()
    q = b.subscribe()
    await consume_flagged(FakeConsumer([b"garbage", assessment(False), assessment(True)]), b)  # type: ignore[arg-type]
    assert q.qsize() == 1
    assert RiskAssessment.model_validate_json(q.get_nowait()).flagged


def test_websocket_requires_valid_token(app_factory):
    with TestClient(app_factory()) as tc, pytest.raises(WebSocketDisconnect) as e:  # noqa: SIM117
        with tc.websocket_connect("/v2/ws/flagged?token=junk"):
            pass
    assert e.value.code == 1008


def test_websocket_streams_flagged_events(app_factory, auth):
    app = app_factory()
    token = auth()["Authorization"].removeprefix("Bearer ")
    broadcaster = app.state.broadcaster
    with TestClient(app) as tc, tc.websocket_connect(f"/v2/ws/flagged?token={token}") as ws:
        deadline = time.monotonic() + 5
        while not broadcaster.subscribers and time.monotonic() < deadline:
            time.sleep(0.01)
        assert tc.portal is not None
        tc.portal.call(broadcaster.publish, '{"txn_id": "t1"}')  # publish on the app's event loop
        assert ws.receive_json() == {"txn_id": "t1"}
    # After the client disconnects its queue is released.
    deadline = time.monotonic() + 5
    while broadcaster.subscribers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not broadcaster.subscribers


async def test_dev_token_route_absent_outside_dev_mode(app_factory):
    app = app_factory(dev_mode=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gw") as c:
        r = await c.post("/dev/token", json={"sub": "x", "role": "admin"})
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"
