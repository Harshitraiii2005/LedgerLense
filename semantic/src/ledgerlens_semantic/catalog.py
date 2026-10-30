"""Business-term catalog: schema + loader. No ML imports here, so validation stays fast."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

# semantic/ (repo layout). Containers override via env vars.
SEMANTIC_DIR = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(os.environ.get("SEMANTIC_CATALOG_PATH", SEMANTIC_DIR / "catalog.yaml"))

# Fields that identify a person. A term exposing any of them must be marked pii.
PII_FIELDS = {"customer_id", "user"}
SOURCES = {"transactions", "agg_daily_category_spend", "agg_merchant_flagged_rate",
           "agg_top_customers", "audit_log"}  # fmt: skip


class Term(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=10)
    source: str
    fields: list[str] = Field(min_length=1)
    formula: str
    filter: dict[str, Any] | None = None  # $match fragment
    pipeline: list[dict[str, Any]] | None = None  # stages applied after the filter
    owner: str
    sensitivity: Literal["pii", "non-pii"]
    synonyms: list[str] = []

    @model_validator(mode="after")
    def check(self) -> "Term":
        if self.source not in SOURCES:
            raise ValueError(f"{self.name}: unknown source collection {self.source!r}")
        if self.filter is None and self.pipeline is None:
            raise ValueError(f"{self.name}: needs a filter or a pipeline")
        if PII_FIELDS & set(self.fields) and self.sensitivity != "pii":
            raise ValueError(f"{self.name}: exposes {PII_FIELDS & set(self.fields)} but is not marked pii")
        return self

    def embedding_text(self) -> str:
        """What gets embedded: the language an analyst might use, not the Mongo syntax."""
        return (
            f"{self.name.replace('_', ' ')}: {self.description} "
            f"Also called: {', '.join(self.synonyms)}. Defined as: {self.formula}."
        )


class Catalog(BaseModel):
    terms: list[Term] = Field(min_length=15)

    @model_validator(mode="after")
    def unique_names(self) -> "Catalog":
        names = [t.name for t in self.terms]
        if dupes := {n for n in names if names.count(n) > 1}:
            raise ValueError(f"duplicate term names: {dupes}")
        return self

    def get(self, name: str) -> Term:
        return next(t for t in self.terms if t.name == name)


def load_catalog(path: Path = CATALOG_PATH) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(path.read_text()))


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return load_catalog()
