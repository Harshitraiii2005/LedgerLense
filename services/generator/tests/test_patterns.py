from collections import Counter
from datetime import UTC, datetime

from generator.patterns import COUNTRY_CURRENCY, FAR_APART, PATTERN_SIZES, TransactionFactory


def generate(n: int, seed: int = 7) -> list:
    f = TransactionFactory(seed=seed)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    out: list = []
    while len(out) < n:
        out.extend(f.next_batch(now))
    return out


def test_suspicious_share_is_about_three_percent():
    rows = generate(30_000)
    share = sum(1 for _, p in rows if p) / len(rows)
    assert 0.025 < share < 0.035, share


def test_all_patterns_appear():
    counts = Counter(p for _, p in generate(30_000) if p)
    assert set(counts) == set(PATTERN_SIZES)


def test_seeded_runs_are_reproducible():
    a = [t.txn_id for t, _ in generate(200, seed=1)]
    b = [t.txn_id for t, _ in generate(200, seed=1)]
    assert a == b


def test_pattern_shapes():
    f = TransactionFactory(seed=3, suspicious_rate=0.99)
    seen: set[str] = set()
    while len(seen) < 3:
        batch = f.next_batch()
        pattern = batch[0][1]
        if pattern is None:
            continue
        seen.add(pattern)
        txns = [t for t, _ in batch]
        assert len(txns) == PATTERN_SIZES[pattern]
        assert len({t.customer_id for t in txns}) == 1
        if pattern == "impossible_geography":
            assert txns[1].country == FAR_APART[txns[0].country]
            assert (txns[1].timestamp - txns[0].timestamp).total_seconds() == 600
        if pattern == "velocity_burst":
            assert (txns[-1].timestamp - txns[0].timestamp).total_seconds() < 60


def test_currency_matches_country():
    for t, _ in generate(500):
        assert t.currency == COUNTRY_CURRENCY[t.country]


def test_customers_are_stable_across_unseeded_runs():
    a, b = TransactionFactory(seed=None), TransactionFactory(seed=None)
    assert a.customers == b.customers
    assert a.merchants == b.merchants
    assert a.next_batch()[0][0].txn_id != b.next_batch()[0][0].txn_id
