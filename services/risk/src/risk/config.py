"""Risk service settings, read from env vars (a ConfigMap + Secret in Kubernetes).

Thresholds are validated at startup: a typo'd ConfigMap must stop the service with a
clear log line, not silently flag everything (or nothing).
"""

from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    kafka_bootstrap: str = "localhost:9092"
    consumer_group: str = "risk"
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "ledgerlens"
    couchbase_url: str = "couchbase://localhost"
    couchbase_user: str = "Administrator"
    couchbase_password: SecretStr = SecretStr("")
    couchbase_bucket: str = "ledgerlens"
    port: int = 8001
    log_level: str = "INFO"

    # VELOCITY: at least `max_txns` transactions (including this one) within `window_s` seconds.
    risk_velocity_window_s: int = Field(20, gt=0, le=3600)
    risk_velocity_max_txns: int = Field(5, ge=2)
    # AMOUNT_ZSCORE: z-score of log(amount) against this customer's history.
    risk_zscore_threshold: float = Field(3.5, gt=0)
    risk_zscore_min_history: int = Field(10, ge=2)
    # COUNTRY_JUMP: a foreign country different from the previous txn's, within `window_s`.
    risk_country_window_s: int = Field(7200, gt=0)
    # Each fired rule adds its weight; the sum is capped at 100.
    risk_weight_velocity: int = Field(50, ge=0, le=100)
    risk_weight_amount: int = Field(60, ge=0, le=100)
    risk_weight_country: int = Field(70, ge=0, le=100)
    risk_flag_threshold: int = Field(50, ge=1, le=100)

    @model_validator(mode="after")
    def threshold_reachable(self) -> Self:
        total = self.risk_weight_velocity + self.risk_weight_amount + self.risk_weight_country
        if total < self.risk_flag_threshold:
            raise ValueError(
                f"risk_flag_threshold={self.risk_flag_threshold} can never be reached "
                f"(sum of rule weights is {total}); nothing would ever be flagged"
            )
        return self
