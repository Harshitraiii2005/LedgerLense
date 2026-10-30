"""Canonical transaction schema shared by every service.

The generator builds these, ingest validates raw Kafka payloads against them,
and downstream services read them back. One model = one contract.
"""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Category = Literal[
    "groceries", "restaurants", "travel", "fuel", "electronics",
    "utilities", "entertainment", "health", "fashion", "cash_withdrawal",
]  # fmt: skip
Channel = Literal["card_present", "online", "mobile", "atm"]
Currency = Literal["USD", "EUR", "GBP", "INR", "JPY", "SGD", "AUD", "CAD"]


class Transaction(BaseModel):
    # extra="forbid": unknown fields are a contract violation and go to the DLQ
    # rather than silently landing in Mongo.
    model_config = ConfigDict(extra="forbid", frozen=True)

    txn_id: str = Field(min_length=8, max_length=64)
    customer_id: str = Field(pattern=r"^C\d{6}$")
    merchant: str = Field(min_length=1, max_length=120)
    category: Category
    # Money is Decimal, never float: 0.1 + 0.2 must equal 0.3 in a bank.
    amount: Decimal = Field(gt=0, le=Decimal("1000000"), max_digits=12, decimal_places=2)
    currency: Currency
    country: str = Field(pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
    channel: Channel
    timestamp: AwareDatetime  # naive datetimes are rejected; everything is UTC-aware


class RiskReason(BaseModel):
    code: str  # e.g. VELOCITY, AMOUNT_ZSCORE, COUNTRY_JUMP
    detail: str


class RiskAssessment(BaseModel):
    """Output of the risk service: published to transactions.flagged and $set as `risk` on the txn doc."""

    txn_id: str
    customer_id: str
    score: int = Field(ge=0, le=100)
    flagged: bool
    reasons: list[RiskReason]
    scored_at: AwareDatetime
    transaction: Transaction
