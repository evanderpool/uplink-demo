"""Hybrid retrieval: embedder contract, RRF math, vector store, and the
flag that keeps the live path pure-BM25 until hybrid is explicitly enabled.

Everything here uses the dependency-free HashEmbedder, so the whole hybrid
pipeline is exercised with no model download and no API key."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink import vectors  # noqa: E402
from uplink.embeddings import (EmbedderUnavailable, HashEmbedder,  # noqa: E402
                               VoyageEmbedder, get_embedder)
from uplink.hybrid import hybrid_search, retrieve, rrf_fuse  # noqa: E402
from uplink.indexer import index_folder  # noqa: E402
from uplink.search import search  # noqa: E402


@pytest.fixture
def indexed(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "alpha.md").write_text(
        "# Alpha\n\n## Revenue\n\nAlpha Corp revenue was 100 in 2021.",
        encoding="utf-8")
    (root / "beta.md").write_text(
        "# Beta\n\n## Earnings\n\nBeta Inc earnings reached 200 in 2022.",
        encoding="utf-8")
    (root / "gamma.md").write_text(
        "# Gamma\n\n## Costs\n\nGamma LLC spent 50 on research in 2023.",
        encoding="utf-8")
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="test")
    return db_path


# ---------------------------------------------------------------- embedder

def test_hash_embedder_is_deterministic_and_normalised():
    e = HashEmbedder(dim=64)
    a = e.embed(["revenue growth in 2021"])[0]
    b = e.embed(["revenue growth in 2021"])[0]
    assert a == b                       # deterministic
    assert len(a) == 64
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6       # unit length


def test_get_embedder_defaults_to_hash_and_reads_env(monkeypatch):
    monkeypatch.delenv("UPLINK_EMBEDDER", raising=False)
    assert get_embedder().name.startswith("hash")
    monkeypatch.setenv("UPLINK_EMBEDDER", "nonsense")
    assert get_embedder().name.startswith("hash")   # unknown → safe fallback
    monkeypatch.setenv("UPLINK_EMBEDDER", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(EmbedderUnavailable):
        get_embedder()                  # named but no key → explicit error


# --------------------------------------------------------------------- RRF

def test_rrf_rewards_agreement_and_needs_no_score_scaling():
    # 'b' is ranked #1 by BOTH lists; 'a' is in both but lower; 'c'/'d' each
    # appear in only one list.
    fused = rrf_fuse([["b", "a", "c"], ["b", "d", "a"]])
    assert fused["b"] > fused["a"]      # agreed-on top wins
    assert fused["a"] > fused["d"]      # in both lists beats in one
    assert fused["d"] > 0               # single-list doc still scores
    assert set(fused) == {"a", "b", "c", "d"}


# ----------------------------------------------------------- vector store

def test_build_and_search_vectors_roundtrip(indexed):
    e = HashEmbedder(dim=64)
    n = vectors.build_vectors(indexed, e)
    assert n >= 3                       # one per chunk
    # re-running embeds nothing new (idempotent)
    assert vectors.build_vectors(indexed, e) == 0
    qv = e.embed(["research costs"])[0]
    hits = vectors.vector_search(indexed, qv, k=3)
    assert hits and hits[0].path.endswith(".md")
    assert all(-1.01 <= h.score <= 1.01 for h in hits)  # cosine range


def test_vector_search_honours_scope(indexed):
    e = HashEmbedder(dim=64)
    vectors.build_vectors(indexed, e)
    qv = e.embed(["earnings"])[0]
    only_beta = vectors.vector_search(indexed, qv, k=5,
                                      include=[("test", "beta.md")])
    assert {h.path for h in only_beta} == {"beta.md"}
    assert vectors.vector_search(indexed, qv, k=5, include=[]) == []  # none selected


# ------------------------------------------------------------- hybrid + flag

def test_hybrid_degrades_to_bm25_when_no_vectors(indexed):
    # No vectors built: hybrid must still return BM25 results, not nothing.
    e = HashEmbedder(dim=64)
    hits = hybrid_search(indexed, "revenue", e, k=3)
    assert hits and any("alpha" in h.path for h in hits)


def test_hybrid_fuses_when_vectors_exist(indexed):
    e = HashEmbedder(dim=64)
    vectors.build_vectors(indexed, e)
    hits = hybrid_search(indexed, "research costs 2023", e, k=3)
    assert hits
    # gamma is the research/costs doc — it should surface.
    assert any("gamma" in h.path for h in hits)


def test_retrieve_flag_off_is_pure_bm25(indexed, monkeypatch):
    monkeypatch.delenv("UPLINK_HYBRID", raising=False)
    got = retrieve(indexed, "revenue", k=3)
    expected = search(indexed, "revenue", k=3)
    assert [h.path for h in got] == [h.path for h in expected]


def test_retrieve_flag_on_without_vectors_never_crashes(indexed, monkeypatch):
    monkeypatch.setenv("UPLINK_HYBRID", "1")
    monkeypatch.setenv("UPLINK_EMBEDDER", "hash")
    # No vectors built yet — must fall back to BM25, not raise.
    got = retrieve(indexed, "revenue", k=3)
    assert got and any("alpha" in h.path for h in got)


def test_mismatched_embedder_degrades_to_bm25(indexed):
    """The silent-garbage bug the review caught: vectors built by one model,
    queried by another. Cosine would be meaningless, so the vector side must
    be skipped and only BM25 answer — never fuse across models."""
    from uplink.vectors import embedder_matches
    vectors.build_vectors(indexed, HashEmbedder(dim=64))     # stored: hash-64
    other = HashEmbedder(dim=32)                             # name: hash-32
    assert embedder_matches(indexed, "hash-64") is True
    assert embedder_matches(indexed, other.name) is False
    hits = hybrid_search(indexed, "revenue", other, k=3)     # must not crash/garble
    assert hits and any("alpha" in h.path for h in hits)


def test_vector_search_rejects_wrong_dimension(indexed):
    """Defense in depth: a wrong-dimension query is refused, not silently
    truncated by zip()."""
    vectors.build_vectors(indexed, HashEmbedder(dim=64))
    wrong = HashEmbedder(dim=32).embed(["revenue"])[0]
    with pytest.raises(ValueError):
        vectors.vector_search(indexed, wrong, k=3)
