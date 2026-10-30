import pytest
import yaml
from ledgerlens_semantic.catalog import CATALOG_PATH, Catalog, load_catalog
from pydantic import ValidationError


def test_catalog_is_valid():
    catalog = load_catalog()
    assert len(catalog.terms) >= 15
    for name in ("high_risk_transaction", "active_customer", "monthly_spend", "flagged_rate"):
        catalog.get(name)


def test_pii_fields_force_pii_sensitivity():
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    term = next(t for t in raw["terms"] if t["name"] == "active_customer")
    term["sensitivity"] = "non-pii"
    with pytest.raises(ValidationError, match="not marked pii"):
        Catalog.model_validate(raw)


def test_term_needs_filter_or_pipeline():
    raw = yaml.safe_load(CATALOG_PATH.read_text())
    term = raw["terms"][0]
    term.pop("filter", None)
    term.pop("pipeline", None)
    with pytest.raises(ValidationError, match="needs a filter or a pipeline"):
        Catalog.model_validate(raw)


@pytest.fixture(scope="module")
def index_dir(tmp_path_factory):
    from ledgerlens_semantic.index import build

    try:
        return build(out_dir=tmp_path_factory.mktemp("idx"))
    except OSError as e:  # model download failed (offline / no HF access)
        pytest.skip(f"embedding model unavailable: {e}")


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("how many risky transactions are there", "high_risk_transaction"),
        ("what share of payments got flagged", "flagged_rate"),
        ("which customers were active this month", "active_customer"),
        ("total spend per month", "monthly_spend"),
        ("merchants with the most fraud alerts", "merchant_flagged_rate"),
    ],
)
def test_retrieval_top3(index_dir, question, expected):
    from ledgerlens_semantic.index import search

    hits = search(question, k=3, index_dir=index_dir)
    assert expected in [h.term.name for h in hits], [(h.term.name, round(h.score, 3)) for h in hits]
