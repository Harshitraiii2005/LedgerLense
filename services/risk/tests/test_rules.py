import math
import statistics
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from ledgerlens_common.models import Transaction
from pydantic import ValidationError
from risk.config import Settings
from risk.rules import (
    RECENT_MAX,
    amount_zscore,
    apply,
    assess,
    country_jump,
    find_recent,
    from_recent,
    new_profile,
    velocity,
)

CFG = Settings()
T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)
_n = 0


def txn(amount: str = "50.00", country: str = "US", ts: datetime = T0, customer: str = "C000001"):
    global _n
    _n += 1
    return Transaction(
        txn_id=f"txn{_n:09d}", customer_id=customer, merchant="Acme", category="groceries",
        amount=Decimal(amount), currency="USD", country=country, channel="online", timestamp=ts,
    )  # fmt: skip


def history(amounts: list[str], start: datetime = T0, gap_s: int = 3600, country: str = "US"):
    """Profile after feeding txns spaced `gap_s` apart (far enough apart to never trip velocity)."""
    p = new_profile("C000001")
    for i, a in enumerate(amounts):
        p = apply(p, assess(txn(a, country, start + timedelta(seconds=i * gap_s)), p, CFG))
    return p


# --- VELOCITY ------------------------------------------------------------------------------


def burst(n: int, gap_s: int):
    p = new_profile("C000001")
    for i in range(n):
        p = apply(p, assess(txn(ts=T0 + timedelta(seconds=i * gap_s)), p, CFG))
    return p


def test_velocity_fires_at_threshold():
    # 4 prior txns 3s apart + this one = 5 within 20s
    p = burst(CFG.risk_velocity_max_txns - 1, 3)
    r = velocity(txn(ts=T0 + timedelta(seconds=12)), p, CFG)
    assert r is not None and r.code == "VELOCITY"


def test_velocity_quiet_below_threshold():
    p = burst(CFG.risk_velocity_max_txns - 2, 3)
    assert velocity(txn(ts=T0 + timedelta(seconds=9)), p, CFG) is None


def test_velocity_ignores_txns_outside_window():
    p = burst(4, 3)  # at T0..T0+9s
    later = T0 + timedelta(seconds=9 + CFG.risk_velocity_window_s + 1)
    assert velocity(txn(ts=later), p, CFG) is None


# --- AMOUNT_ZSCORE -------------------------------------------------------------------------

NORMAL = ["40.00", "55.00", "48.00", "62.00", "35.00", "50.00", "45.00", "58.00", "52.00", "41.00", "60.00"]


def test_zscore_needs_min_history():
    p = history(NORMAL[: CFG.risk_zscore_min_history - 1])
    assert amount_zscore(txn("5000.00"), p, CFG) is None


def test_zscore_fires_on_spike():
    p = history(NORMAL)
    r = amount_zscore(txn("2000.00"), p, CFG)
    assert r is not None and r.code == "AMOUNT_ZSCORE"


def test_zscore_quiet_for_ordinary_amount_and_small_amounts():
    p = history(NORMAL)
    assert amount_zscore(txn("70.00"), p, CFG) is None
    assert amount_zscore(txn("1.00"), p, CFG) is None  # only unusually *high* spend is a signal


def test_zscore_zero_variance_does_not_divide_by_zero():
    p = history(["50.00"] * 12)
    assert amount_zscore(txn("5000.00"), p, CFG) is None


def test_welford_matches_statistics_module():
    p = history(NORMAL)
    logs = [math.log(float(a)) for a in NORMAL]
    assert p["txn_count"] == len(NORMAL)
    assert p["log_mean"] == pytest.approx(statistics.fmean(logs))
    assert p["log_m2"] / (len(NORMAL) - 1) == pytest.approx(statistics.variance(logs))


# --- COUNTRY_JUMP --------------------------------------------------------------------------


def test_country_jump_fires_for_far_country_soon_after():
    p = history(["50.00"])  # home US, last txn at T0
    r = country_jump(txn(country="SG", ts=T0 + timedelta(minutes=10)), p, CFG)
    assert r is not None and r.code == "COUNTRY_JUMP" and "US -> SG" in r.detail


def test_country_jump_quiet_after_window():
    p = history(["50.00"])
    later = T0 + timedelta(seconds=CFG.risk_country_window_s + 1)
    assert country_jump(txn(country="SG", ts=later), p, CFG) is None


def test_returning_home_is_not_a_jump():
    p = history(["50.00"])
    p = apply(p, assess(txn(country="SG", ts=T0 + timedelta(minutes=10)), p, CFG))
    assert country_jump(txn(country="US", ts=T0 + timedelta(minutes=20)), p, CFG) is None


def test_no_history_no_jump():
    assert country_jump(txn(country="SG"), new_profile("C000001"), CFG) is None


# --- scoring, profile update, idempotency --------------------------------------------------


def test_clean_txn_scores_zero():
    a = assess(txn(), history(NORMAL), CFG)
    assert (a.score, a.flagged, a.reasons) == (0, False, [])


def test_multiple_rules_add_up_and_cap_at_100():
    p = history(NORMAL)  # last txn: US, at T0 + 10h
    a = assess(txn("5000.00", "SG", T0 + timedelta(hours=10, minutes=5)), p, CFG)
    assert {r.code for r in a.reasons} == {"AMOUNT_ZSCORE", "COUNTRY_JUMP"}
    assert a.score == 100 and a.flagged


def test_flag_threshold_is_respected():
    strict = Settings(risk_flag_threshold=80)
    p = history(["50.00"])
    a = assess(txn(country="SG", ts=T0 + timedelta(minutes=10)), p, strict)
    assert a.score == strict.risk_weight_country and not a.flagged


def test_apply_tracks_counters_and_trims_recent():
    p = burst(RECENT_MAX + 5, 600)
    assert len(p["recent"]) == RECENT_MAX
    assert p["home_country"] == "US" and p["last_country"] == "US"
    assert p["txn_count"] == RECENT_MAX + 5


def test_redelivered_txn_reuses_stored_assessment():
    p = history(["50.00"])
    t = txn(country="SG", ts=T0 + timedelta(minutes=10))
    first = assess(t, p, CFG)
    p2 = apply(p, first)
    prior = find_recent(p2, t.txn_id)
    assert prior is not None
    again = from_recent(t, prior)
    assert again.score == first.score and again.reasons == first.reasons
    assert p2["flagged_count"] == 1


# --- config validation (the incident exercise relies on this failing loudly) ---------------


@pytest.mark.parametrize(
    "bad",
    [
        {"risk_flag_threshold": 0},
        {"risk_velocity_window_s": -5},
        {"risk_zscore_threshold": "high"},
        # weights sum to 30 < threshold 50: nothing could ever be flagged
        dict(risk_weight_velocity=10, risk_weight_amount=10, risk_weight_country=10, risk_flag_threshold=50),
    ],
)
def test_invalid_config_is_rejected(bad):
    with pytest.raises(ValidationError):
        Settings(**bad)


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("RISK_FLAG_THRESHOLD", "70")
    assert Settings().risk_flag_threshold == 70
