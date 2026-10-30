"""Synthetic transaction model. Pure functions, no I/O, so it's deterministic under a seed.

Normal traffic: each customer has a home country, a preferred currency and a
typical spend (log-normal). ~3% of transactions belong to an injected suspicious
pattern; the pattern name travels in a Kafka header (not in the payload) so the
risk service can be evaluated against ground truth without seeing the label.
"""

import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args

from faker import Faker

from ledgerlens_common.models import Category, Channel, Transaction

CATEGORIES: tuple[Category, ...] = get_args(Category)
CHANNELS: tuple[Channel, ...] = get_args(Channel)
# Country -> local currency. Pairs in FAR_APART are used for impossible geography.
COUNTRY_CURRENCY = {"US": "USD", "GB": "GBP", "DE": "EUR", "FR": "EUR", "IN": "INR",
                    "JP": "JPY", "SG": "SGD", "AU": "AUD", "CA": "CAD"}  # fmt: skip
FAR_APART = {"US": "SG", "GB": "AU", "DE": "JP", "FR": "AU", "IN": "CA",
             "JP": "GB", "SG": "US", "AU": "DE", "CA": "IN"}  # fmt: skip

# Pattern name -> number of transactions it emits.
PATTERN_SIZES = {"velocity_burst": 8, "amount_spike": 1, "impossible_geography": 2}


@dataclass(frozen=True)
class Customer:
    customer_id: str
    country: str
    mean_log_amount: float  # log of the customer's typical spend


Labelled = tuple[Transaction, str | None]  # (txn, suspicious pattern name or None)


class TransactionFactory:
    def __init__(self, seed: int | None = None, customers: int = 500, suspicious_rate: float = 0.03):
        self.rng = random.Random(seed)
        self.fake = Faker()
        self.fake.seed_instance(seed)
        self.customers = [
            Customer(
                customer_id=f"C{i:06d}",
                country=self.rng.choice(list(COUNTRY_CURRENCY)),
                mean_log_amount=self.rng.uniform(math.log(15), math.log(250)),
            )
            for i in range(1, customers + 1)
        ]
        self.merchants = {c: [self.fake.company() for _ in range(15)] for c in CATEGORIES}
        # A pattern emits k transactions, so to make `suspicious_rate` of all
        # *transactions* suspicious, solve f = pK / (pK + 1 - p) for the trigger prob p.
        k = sum(PATTERN_SIZES.values()) / len(PATTERN_SIZES)
        self.trigger_prob = suspicious_rate / (k * (1 - suspicious_rate) + suspicious_rate)

    def _txn(self, c: Customer, ts: datetime, amount: float, country: str | None = None) -> Transaction:
        category = self.rng.choice(CATEGORIES)
        country = country or c.country
        return Transaction(
            txn_id=uuid.UUID(int=self.rng.getrandbits(128), version=4).hex,
            customer_id=c.customer_id,
            merchant=self.rng.choice(self.merchants[category]),
            category=category,
            amount=Decimal(f"{min(amount, 999_999):.2f}").max(Decimal("0.01")),
            currency=COUNTRY_CURRENCY[country],
            country=country,
            channel="atm" if category == "cash_withdrawal" else self.rng.choice(CHANNELS[:3]),
            timestamp=ts,
        )

    def _normal_amount(self, c: Customer) -> float:
        return self.rng.lognormvariate(c.mean_log_amount, 0.6)

    def next_batch(self, now: datetime | None = None) -> list[Labelled]:
        """One normal transaction, or every transaction of one suspicious pattern."""
        now = now or datetime.now(UTC)
        c = self.rng.choice(self.customers)
        if self.rng.random() >= self.trigger_prob:
            return [(self._txn(c, now, self._normal_amount(c)), None)]

        pattern = self.rng.choice(list(PATTERN_SIZES))
        if pattern == "velocity_burst":
            # Many small transactions seconds apart: classic card-testing behaviour.
            txns = [self._txn(c, now + timedelta(seconds=i * 3), self._normal_amount(c) / 3)
                    for i in range(PATTERN_SIZES[pattern])]  # fmt: skip
        elif pattern == "amount_spike":
            txns = [self._txn(c, now, math.exp(c.mean_log_amount) * self.rng.uniform(15, 40))]
        else:  # impossible_geography: home country, then a far-away country 10 minutes later
            txns = [
                self._txn(c, now, self._normal_amount(c)),
                self._txn(c, now + timedelta(minutes=10), self._normal_amount(c), FAR_APART[c.country]),
            ]
        return [(t, pattern) for t in txns]
