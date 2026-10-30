"""Risk rules as pure functions over (transaction, customer profile, settings).

No I/O here: the store loads the profile, these functions decide, the store saves.
That keeps every rule unit-testable and the scoring explainable line by line.

The profile is a plain JSON document (it lives in Couchbase as `profile::<customer_id>`).
"""

import math
from datetime import UTC, datetime
from typing import TypedDict

from ledgerlens_common.models import RiskAssessment, RiskReason, Transaction

from risk.config import Settings

# Trade-off: the idempotency window is the last RECENT_MAX txns per customer; a redelivery
# older than that would be double counted. Fine for crash-and-retry; use a marker doc if not.
RECENT_MAX = 20


class RecentTxn(TypedDict):
    txn_id: str
    ts: str  # ISO-8601
    country: str
    score: int
    flagged: bool
    reasons: list[dict[str, str]]
    scored_at: str


class Profile(TypedDict):
    type: str  # always "profile"; lets the SQL++ index filter on document type
    customer_id: str
    home_country: str | None  # first country seen for this customer
    txn_count: int
    # Welford's online mean / sum of squared deviations, over log(amount).
    log_mean: float
    log_m2: float
    last_country: str | None
    last_ts: str | None
    recent: list[RecentTxn]
    flagged_count: int
    max_score: int
    updated_at: str | None


def new_profile(customer_id: str) -> Profile:
    return Profile(
        type="profile",
        customer_id=customer_id,
        home_country=None,
        txn_count=0,
        log_mean=0.0,
        log_m2=0.0,
        last_country=None,
        last_ts=None,
        recent=[],
        flagged_count=0,
        max_score=0,
        updated_at=None,
    )


def _seconds_between(earlier_iso: str, later: datetime) -> float:
    return (later - datetime.fromisoformat(earlier_iso)).total_seconds()


def velocity(txn: Transaction, p: Profile, cfg: Settings) -> RiskReason | None:
    window = cfg.risk_velocity_window_s
    # Count prior txns in [ts - window, ts], plus this one. Uses event time, not arrival time.
    n = 1 + sum(1 for r in p["recent"] if 0 <= _seconds_between(r["ts"], txn.timestamp) <= window)
    if n >= cfg.risk_velocity_max_txns:
        return RiskReason(code="VELOCITY", detail=f"{n} transactions within {window}s")
    return None


def amount_zscore(txn: Transaction, p: Profile, cfg: Settings) -> RiskReason | None:
    n = p["txn_count"]
    if n < cfg.risk_zscore_min_history:
        return None  # not enough history to know what "normal" is
    std = math.sqrt(p["log_m2"] / (n - 1))
    if std == 0:
        return None
    # log scale: spend is roughly log-normal, so a raw z-score would flag ordinary big purchases.
    z = (math.log(txn.amount) - p["log_mean"]) / std
    if z >= cfg.risk_zscore_threshold:
        typical = math.exp(p["log_mean"])
        return RiskReason(code="AMOUNT_ZSCORE", detail=f"z={z:.1f} vs typical {typical:.2f} {txn.currency}")
    return None


def country_jump(txn: Transaction, p: Profile, cfg: Settings) -> RiskReason | None:
    if p["last_country"] is None or p["last_ts"] is None:
        return None
    # Returning home is never a jump; a new foreign country shortly after another country is.
    if txn.country in (p["home_country"], p["last_country"]):
        return None
    gap = _seconds_between(p["last_ts"], txn.timestamp)
    if 0 <= gap <= cfg.risk_country_window_s:
        return RiskReason(
            code="COUNTRY_JUMP", detail=f"{p['last_country']} -> {txn.country} in {gap / 60:.0f} min"
        )
    return None


def assess(txn: Transaction, p: Profile, cfg: Settings, now: datetime | None = None) -> RiskAssessment:
    weights = {
        "VELOCITY": cfg.risk_weight_velocity,
        "AMOUNT_ZSCORE": cfg.risk_weight_amount,
        "COUNTRY_JUMP": cfg.risk_weight_country,
    }
    reasons = [r for rule in (velocity, amount_zscore, country_jump) if (r := rule(txn, p, cfg))]
    score = min(100, sum(weights[r.code] for r in reasons))
    return RiskAssessment(
        txn_id=txn.txn_id,
        customer_id=txn.customer_id,
        score=score,
        flagged=score >= cfg.risk_flag_threshold,
        reasons=reasons,
        scored_at=now or datetime.now(UTC),
        transaction=txn,
    )


def find_recent(p: Profile, txn_id: str) -> RecentTxn | None:
    return next((r for r in p["recent"] if r["txn_id"] == txn_id), None)


def from_recent(txn: Transaction, r: RecentTxn) -> RiskAssessment:
    """Rebuild the original assessment of an already-scored txn (redelivery after a crash)."""
    return RiskAssessment(
        txn_id=txn.txn_id,
        customer_id=txn.customer_id,
        score=r["score"],
        flagged=r["flagged"],
        reasons=[RiskReason(**x) for x in r["reasons"]],
        scored_at=datetime.fromisoformat(r["scored_at"]),
        transaction=txn,
    )


def apply(p: Profile, a: RiskAssessment) -> Profile:
    """Return the profile updated with this transaction (new dict; input untouched)."""
    txn = a.transaction
    n = p["txn_count"] + 1
    x = math.log(txn.amount)
    delta = x - p["log_mean"]
    mean = p["log_mean"] + delta / n
    recent = [
        *p["recent"],
        RecentTxn(
            txn_id=txn.txn_id,
            ts=txn.timestamp.isoformat(),
            country=txn.country,
            score=a.score,
            flagged=a.flagged,
            reasons=[r.model_dump() for r in a.reasons],
            scored_at=a.scored_at.isoformat(),
        ),
    ][-RECENT_MAX:]
    return Profile(
        type="profile",
        customer_id=p["customer_id"],
        home_country=p["home_country"] or txn.country,
        txn_count=n,
        log_mean=mean,
        log_m2=p["log_m2"] + delta * (x - mean),
        last_country=txn.country,
        last_ts=txn.timestamp.isoformat(),
        recent=recent,
        flagged_count=p["flagged_count"] + int(a.flagged),
        max_score=max(p["max_score"], a.score),
        updated_at=a.scored_at.isoformat(),
    )
