"""Couchbase-backed customer profiles: KV read-modify-write with CAS, plus one SQL++ report."""

import logging
import random
import time
from datetime import timedelta
from typing import Any, cast

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import CasMismatchException, DocumentExistsException, DocumentNotFoundException
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import ClusterOptions, QueryOptions, ReplaceOptions
from ledgerlens_common.models import RiskAssessment, Transaction

from risk.config import Settings
from risk.rules import Profile, apply, assess, find_recent, from_recent, new_profile

log = logging.getLogger("risk.store")
CAS_RETRIES = 10


class ProfileStore:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        auth = PasswordAuthenticator(cfg.couchbase_user, cfg.couchbase_password.get_secret_value())
        self.cluster = Cluster(cfg.couchbase_url, ClusterOptions(auth))
        self.cluster.wait_until_ready(timedelta(seconds=30))
        self.bucket_name = cfg.couchbase_bucket
        self.coll = self.cluster.bucket(cfg.couchbase_bucket).default_collection()

    def ensure_index(self) -> None:
        # Partial GSI index: only profile docs, ordered the way the report sorts.
        self.cluster.query(
            f"CREATE INDEX idx_profile_risk IF NOT EXISTS ON `{self.bucket_name}`"
            "(flagged_count DESC, max_score DESC) WHERE type = 'profile'"
        ).execute()

    def get(self, customer_id: str) -> tuple[Profile | None, int | None]:
        try:
            res = self.coll.get(f"profile::{customer_id}")
        except DocumentNotFoundException:
            return None, None
        return cast(Profile, res.content_as[dict]), res.cas

    def score_and_update(self, txn: Transaction) -> RiskAssessment:
        """Score txn against the stored profile and save the updated profile atomically.

        Optimistic concurrency: read (profile, CAS), compute, write only if the CAS is
        unchanged. If another writer got in first, re-read and recompute. This is what
        keeps two consumers (e.g. during a Kafka rebalance) from losing each other's updates.
        """
        key = f"profile::{txn.customer_id}"
        for attempt in range(CAS_RETRIES):
            profile, cas = self.get(txn.customer_id)
            profile = profile or new_profile(txn.customer_id)
            if prior := find_recent(profile, txn.txn_id):
                return from_recent(txn, prior)  # already scored: redelivered message
            assessment = assess(txn, profile, self.cfg)
            updated = apply(profile, assessment)
            try:
                if cas is None:
                    self.coll.insert(key, updated)  # fails if someone created it meanwhile
                else:
                    self.coll.replace(key, updated, ReplaceOptions(cas=cas))
                return assessment
            except (CasMismatchException, DocumentExistsException):
                log.info("cas conflict, retrying", extra={"fields": {"customer_id": txn.customer_id}})
                # Jittered exponential backoff so competing writers stop colliding in lockstep.
                time.sleep(random.uniform(0, 0.005 * 2**attempt))  # noqa: S311 - jitter, not crypto
        # Caller treats this as a failed batch: no commit, records are re-read and retried.
        raise RuntimeError(f"gave up on {key} after {CAS_RETRIES} CAS conflicts")

    def dry_run(self, txn: Transaction) -> RiskAssessment:
        profile, _ = self.get(txn.customer_id)
        return assess(txn, profile or new_profile(txn.customer_id), self.cfg)

    def top_risky(self, limit: int) -> list[dict[str, Any]]:
        # Bucket name comes from our own config, not user input; `limit` is a bound parameter.
        statement = (
            "SELECT p.customer_id, p.home_country, p.txn_count, p.flagged_count, p.max_score, "  # noqa: S608
            "ROUND(p.flagged_count / p.txn_count, 4) AS flagged_rate "
            f"FROM `{self.bucket_name}` p "
            "WHERE p.type = 'profile' AND p.flagged_count > 0 "
            "ORDER BY p.flagged_count DESC, p.max_score DESC LIMIT $limit"
        )
        # REQUEST_PLUS: wait for the index to include writes made before this query.
        opts = QueryOptions(
            named_parameters={"limit": limit}, scan_consistency=QueryScanConsistency.REQUEST_PLUS
        )
        return list(self.cluster.query(statement, opts).rows())

    def ping(self) -> None:
        self.coll.exists("health::ping")  # cheap KV round trip; raises if the cluster is unreachable
