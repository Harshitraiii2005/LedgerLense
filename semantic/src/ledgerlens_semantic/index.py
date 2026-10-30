"""Embed catalog terms with sentence-transformers into a FAISS index, and search it.

Build:  uv run ledgerlens-build-index      (or: uv run python semantic/build_index.py)
Writes: semantic/index/terms.faiss + semantic/index/terms.json (gitignored, rebuilt from catalog.yaml)
"""

import hashlib
import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ledgerlens_semantic.catalog import CATALOG_PATH, SEMANTIC_DIR, Term, default_catalog, load_catalog

# torch and faiss-cpu each bundle their own OpenMP runtime; on macOS, two runtimes running thread
# pools in one process segfault. One thread is plenty for ~20 short texts. Must be set before either loads.
os.environ.setdefault("OMP_NUM_THREADS", "1")

MODEL_NAME = os.environ.get("SEMANTIC_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
INDEX_DIR = Path(os.environ.get("SEMANTIC_INDEX_DIR", SEMANTIC_DIR / "index"))


@dataclass(frozen=True)
class Hit:
    term: Term
    score: float  # cosine similarity, higher = closer


@lru_cache(maxsize=1)
def _model() -> Any:
    # Imported lazily: torch takes seconds to import and isn't needed for catalog validation.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME, device="cpu")


def _embed(texts: list[str]) -> Any:
    # Normalised vectors + inner-product index = cosine similarity.
    return _model().encode(texts, normalize_embeddings=True, convert_to_numpy=True).astype("float32")


def build(catalog_path: Path = CATALOG_PATH, out_dir: Path = INDEX_DIR) -> Path:
    import faiss

    catalog = load_catalog(catalog_path)
    vectors = _embed([t.embedding_text() for t in catalog.terms])
    # Trade-off: exact (flat) search; ~20 terms. Switch to IVF/HNSW only at tens of thousands of terms.
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    out_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(out_dir / "terms.faiss"))
    meta = {
        "model": MODEL_NAME,
        "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "terms": [t.name for t in catalog.terms],  # row i of the index = terms[i]
    }
    (out_dir / "terms.json").write_text(json.dumps(meta, indent=2))
    print(f"indexed {len(catalog.terms)} terms with {MODEL_NAME} -> {out_dir}")
    return out_dir


def main() -> None:
    """Console-script entry point (its return value becomes the exit code, so return None)."""
    build()


@lru_cache(maxsize=4)
def _load_index(index_dir: Path) -> tuple[Any, list[str]]:
    import faiss

    meta = json.loads((index_dir / "terms.json").read_text())
    if meta["catalog_sha256"] != hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest():
        raise RuntimeError("semantic index is stale: catalog.yaml changed, run ledgerlens-build-index")
    return faiss.read_index(str(index_dir / "terms.faiss")), meta["terms"]


def search(query: str, k: int = 3, index_dir: Path = INDEX_DIR) -> list[Hit]:
    """Top-k catalog terms for a natural-language question."""
    index, names = _load_index(index_dir)
    scores, rows = index.search(_embed([query]), k)
    catalog = default_catalog()
    return [Hit(catalog.get(names[r]), float(s)) for s, r in zip(scores[0], rows[0], strict=True) if r >= 0]
