"""HTTP API tests with an in-memory store (no Couchbase, no Kafka)."""

import json

import pytest

from risk.app import create_app, load_settings
from risk.config import Settings
from risk.rules import apply, assess, new_profile

CFG = Settings()
TXN = {
    "txn_id": "a" * 32, "customer_id": "C000001", "merchant": "Acme", "category": "travel", "amount": "80.00",
    "currency": "USD", "country": "SG", "channel": "online", "timestamp": "2026-01-01T12:10:00Z",
}  # fmt: skip


class FakeStore:
    def __init__(self):
        from ledgerlens_common.models import Transaction

        home = Transaction.model_validate(TXN | {"txn_id": "b" * 32, "country": "US",
                                                 "timestamp": "2026-01-01T12:00:00Z"})  # fmt: skip
        p = new_profile("C000001")
        self.profiles = {"C000001": apply(p, assess(home, p, CFG))}
        self.writes = 0

    def get(self, customer_id):
        return self.profiles.get(customer_id), 1

    def dry_run(self, txn):
        return assess(txn, self.profiles.get(txn.customer_id) or new_profile(txn.customer_id), CFG)

    def top_risky(self, limit):
        return [{"customer_id": "C000001", "flagged_count": 3}][:limit]

    def ping(self):
        pass


@pytest.fixture
def client():
    store = FakeStore()
    app = create_app(CFG, store=store, start_consumer=False)
    return app.test_client(), store


def test_live(client):
    c, _ = client
    assert c.get("/health/live").json == {"status": "ok"}


def test_ready_without_consumer(client):
    c, _ = client
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json["checks"] == {"couchbase": "ok"}


def test_score_is_a_dry_run(client):
    c, store = client
    before = json.dumps(store.profiles)
    r = c.post("/score", data=json.dumps(TXN), headers={"X-Request-ID": "rid-42"})
    assert r.status_code == 200
    assert r.json["flagged"] is True
    assert [x["code"] for x in r.json["reasons"]] == ["COUNTRY_JUMP"]
    assert r.headers["X-Request-ID"] == "rid-42"
    assert json.dumps(store.profiles) == before  # nothing written


def test_score_validation_error_uses_error_schema(client):
    c, _ = client
    r = c.post("/score", data=json.dumps(TXN | {"amount": "-1"}), headers={"X-Request-ID": "rid-7"})
    assert r.status_code == 422
    assert set(r.json) == {"code", "message", "request_id"}
    assert r.json["code"] == "validation_error" and r.json["request_id"] == "rid-7"


def test_customer_profile_and_404(client):
    c, _ = client
    assert c.get("/customers/C000001/risk").json["home_country"] == "US"
    r = c.get("/customers/C999999/risk")
    assert r.status_code == 404 and r.json["code"] == "not_found" and r.json["request_id"]


def test_unknown_route_uses_error_schema(client):
    c, _ = client
    r = c.get("/nope")
    assert r.status_code == 404 and set(r.json) == {"code", "message", "request_id"}


def test_top_risky_limit_validation(client):
    c, _ = client
    assert c.get("/reports/top-risky?limit=1").json["customers"][0]["customer_id"] == "C000001"
    assert c.get("/reports/top-risky?limit=0").status_code == 400
    assert c.get("/reports/top-risky?limit=abc").status_code == 400


def test_bad_config_exits_with_ex_config(monkeypatch):
    monkeypatch.setenv("RISK_FLAG_THRESHOLD", "not-a-number")
    with pytest.raises(SystemExit) as e:
        load_settings()
    assert e.value.code == 78
