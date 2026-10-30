import pytest

from agent.guard import PipelineRejected, validate
from agent.pii import mask


def ok(pipeline, collection="transactions", role="analyst"):
    return validate(collection, pipeline, role, max_rows=100)


def test_valid_pipeline_gets_row_cap():
    p = [{"$match": {"risk.flagged": True}}, {"$count": "n"}]
    assert ok(p) == [*p, {"$limit": 100}]


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$out": "stolen"}],
        [{"$merge": {"into": "transactions"}}],
        [{"$match": {"$where": "sleep(10000)"}}],
        [{"$match": {"$expr": {"$function": {"body": "x", "args": [], "lang": "js"}}}}],
        [{"$group": {"_id": None, "x": {"$accumulator": {"init": "f"}}}}],
        [{"$facet": {"a": [{"$merge": "x"}]}}],  # write stage hidden in a sub-pipeline
        [{"$facet": {"a": [{"$lookup": {"from": "audit_log", "as": "x", "pipeline": []}}]}}],
        [{"$lookup": {"from": "audit_log", "localField": "a", "foreignField": "b", "as": "x"}}],
        [{"$unionWith": "users"}],
        [{"$currentOp": {}}],
        [{"$collStats": {}}],
        [{"$match": {}, "$out": "x"}],  # two operators in one stage
        [],
        "not a list",
        [["$match"]],
    ],
)
def test_rejected(pipeline):
    with pytest.raises(PipelineRejected):
        ok(pipeline)


def test_audit_log_is_admin_only():
    p = [{"$sort": {"timestamp": -1}}]
    with pytest.raises(PipelineRejected):
        ok(p, collection="audit_log", role="analyst")
    assert ok(p, collection="audit_log", role="admin")[-1] == {"$limit": 100}


def test_unknown_collection_rejected():
    with pytest.raises(PipelineRejected):
        ok([{"$match": {}}], collection="system.users")


def test_mask_by_key_and_by_value():
    rows = [
        {"customer_id": "C000123", "total": 5},
        {"who": "C000999", "nested": {"user": "alice-analyst"}},  # renamed field, nested key
        {"merchant": "Acme Ltd", "_id": {"customer_id": "C000001"}},
    ]
    out = mask(rows)
    assert out[0] == {"customer_id": "C0***23", "total": 5}
    assert out[1]["who"] == "C0***99"
    assert out[1]["nested"]["user"] == "al*********st"
    assert out[2]["merchant"] == "Acme Ltd"
    assert out[2]["_id"]["customer_id"] == "C0***01"


def test_slot_filters_ignore_term_words():
    from ledgerlens_semantic.catalog import default_catalog

    from agent.planner import slot_filters

    term = default_catalog().get("impossible_travel")
    assert slot_filters("How many impossible travel cases online?", term) == {"channel": "online"}
    assert slot_filters("cash withdrawals in India paid in INR") == {"country": "IN", "currency": "INR"}


def test_canonical_keys_then_guard():
    from agent.planner import canonical_keys

    p = canonical_keys([{" $match ": {" risk.flagged": True}}, {"$count": "n"}])
    assert ok(p)[0] == {"$match": {"risk.flagged": True}}
    with pytest.raises(PipelineRejected):  # normalising never smuggles a write stage through
        ok(canonical_keys([{" $out": "x"}]))
