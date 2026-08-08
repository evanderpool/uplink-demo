"""Hybrid retrieval: fuse BM25 (keyword) and vector (semantic) rankings.

BM25 scores and cosine similarities live on completely different scales, so
they cannot simply be added. Reciprocal Rank Fusion sidesteps that: it uses
only each document's RANK in each list, so no score calibration is needed and
a strong result in either retriever floats to the top. This is the phase-2
retriever the evaluation harness gates on before/after numbers — it ships
behind a flag, and with no flag (or no vectors) the system is pure BM25,
unchanged.
"""

from __future__ import annotations

import os

from .search import Hit, search
from .vectors import embedder_matches, vector_search

RRF_K = 60  # the standard dampening constant; larger = flatter rank weighting


def _key(h: Hit) -> tuple:
    """A chunk's identity across both retrievers: collection + path + position."""
    return (h.collection, h.path, h.seq)


def rrf_fuse(rankings: list[list], k: int = RRF_K) -> dict:
    """Reciprocal Rank Fusion. Each ranked list contributes 1/(k + rank) to
    every item it contains; returns {item: fused_score}."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return scores


def hybrid_search(db_path, query, embedder, k=8, per_retriever=24, max_text=1200,
                  collection=None, include=None, exclude=None) -> list[Hit]:
    """Retrieve with BM25 and vectors, fuse with RRF, return the top k.

    Both retrievers run under the same scope filters. If the vector side is
    empty (no vectors built, a different embedder built them, or the embedder
    failed), this degrades to the BM25 ranking rather than returning nothing.
    """
    # Each arm must retrieve at least k candidates, or fusing two 24-deep
    # lists could yield fewer than the k requested.
    pool = max(per_retriever, k)
    bm25_hits = search(db_path, query, k=pool, max_text=max_text,
                       collection=collection, include=include, exclude=exclude)

    vec_hits: list[Hit] = []
    # Only run the vector side if the stored vectors were built by THIS
    # embedder — cosine across models/dimensions is meaningless.
    if embedder_matches(db_path, embedder.name):
        try:
            qvec = embedder.embed([query])[0]
            vec_hits = vector_search(db_path, qvec, k=pool, max_text=max_text,
                                     collection=collection, include=include, exclude=exclude)
        except Exception:
            vec_hits = []  # semantic side is best-effort; BM25 still answers

    if not vec_hits:
        return bm25_hits[:k]

    # Keep the richest Hit per identity (BM25's carries the highlighted snippet).
    best: dict = {}
    for h in bm25_hits + vec_hits:
        kk = _key(h)
        if kk not in best or (not best[kk].snippet and h.snippet):
            best[kk] = h

    fused = rrf_fuse([[_key(h) for h in bm25_hits],
                      [_key(h) for h in vec_hits]])
    order = sorted(fused, key=lambda kk: fused[kk], reverse=True)[:k]
    out = []
    for kk in order:
        h = best[kk]
        out.append(Hit(**{**h.__dict__, "score": round(fused[kk], 5)}))
    return out


def retrieve(db_path, query, k=8, max_text=1200, collection=None,
             include=None, exclude=None, embedder=None):
    """The retrieval entry point callers use. Chooses hybrid when it is
    enabled (UPLINK_HYBRID=1) and an embedder is available, else BM25.

    Keeping this decision in one place means the answer path, the web search
    endpoint, and the eval harness all make the same choice, and turning the
    flag off restores identical BM25 behaviour."""
    if os.environ.get("UPLINK_HYBRID") == "1":
        try:
            from .embeddings import get_embedder
            emb = embedder or get_embedder()
            return hybrid_search(db_path, query, emb, k=k, max_text=max_text,
                                 collection=collection, include=include, exclude=exclude)
        except Exception:
            pass  # any embedder problem falls back to BM25, never an outage
    return search(db_path, query, k=k, max_text=max_text,
                  collection=collection, include=include, exclude=exclude)
