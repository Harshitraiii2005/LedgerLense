"""Allowlist validation for LLM-generated aggregation pipelines.

The model's output is untrusted input. Before anything reaches MongoDB we check:
  - the target collection is one analysts may read (audit_log is admin-only)
  - every stage is a known read-only stage (allowlist, not denylist)
  - no forbidden operator appears ANYWHERE in the tree ($where inside $match,
    $function inside $expr, $merge nested in $facet, ...)
  - $lookup / $unionWith only reach allowed collections
Then a hard $limit is appended so a valid pipeline still can't return unbounded rows.
"""

from collections.abc import Iterator
from typing import Any

READ_ONLY_STAGES = {
    "$match", "$group", "$project", "$sort", "$limit", "$skip", "$count", "$unwind",
    "$addFields", "$set", "$unset", "$bucket", "$sortByCount", "$facet", "$lookup", "$unionWith",
    "$replaceRoot",
}  # fmt: skip
# Write stages and server-side JavaScript. Checked at every depth.
FORBIDDEN_OPERATORS = {"$out", "$merge", "$function", "$where", "$accumulator"}
ANALYST_COLLECTIONS = {
    "transactions", "agg_daily_category_spend", "agg_merchant_flagged_rate", "agg_top_customers",
}  # fmt: skip
ADMIN_COLLECTIONS = ANALYST_COLLECTIONS | {"audit_log"}


class PipelineRejected(Exception):
    pass


def _walk_keys(node: Any) -> Iterator[str]:
    """Every dict key at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_keys(item)


def allowed_collections(role: str) -> set[str]:
    return ADMIN_COLLECTIONS if role == "admin" else ANALYST_COLLECTIONS


def _check_stages(stages: Any, allowed: set[str], where: str) -> None:
    if not isinstance(stages, list) or not stages:
        raise PipelineRejected(f"{where}: pipeline must be a non-empty list of stages")
    for i, stage in enumerate(stages):
        if not isinstance(stage, dict) or len(stage) != 1:
            raise PipelineRejected(f"{where} stage {i}: must be an object with exactly one operator")
        ((op, spec),) = stage.items()
        if op not in READ_ONLY_STAGES:
            raise PipelineRejected(f"{where} stage {i}: {op} is not an allowed read-only stage")
        # Sub-pipelines get the same checks, so nothing can hide inside $facet or $lookup.
        if op == "$facet":
            for name, sub in spec.items():
                _check_stages(sub, allowed, f"{where}.$facet.{name}")
        elif op in ("$lookup", "$unionWith"):
            target = spec if isinstance(spec, str) else spec.get("from") or spec.get("coll")
            if target not in allowed:
                raise PipelineRejected(f"{where} stage {i}: {op} into {target!r} is not allowed")
            if isinstance(spec, dict) and "pipeline" in spec:
                _check_stages(spec["pipeline"], allowed, f"{where}.{op}")


def validate(collection: str, pipeline: Any, role: str, max_rows: int) -> list[dict[str, Any]]:
    """Return a safe copy of `pipeline` with a row cap appended, or raise PipelineRejected."""
    allowed = allowed_collections(role)
    if collection not in allowed:
        raise PipelineRejected(f"collection {collection!r} is not readable by role {role!r}")
    _check_stages(pipeline, allowed, "pipeline")
    if bad := FORBIDDEN_OPERATORS & set(_walk_keys(pipeline)):
        raise PipelineRejected(f"forbidden operator(s): {', '.join(sorted(bad))}")
    return [*pipeline, {"$limit": max_rows}]
