"""Head-to-head: BM25 vs hybrid on the golden fixtures.

This is the gate for phase 2. It scores the SAME questions two ways and
prints the deltas plus a per-question diff (which questions each retriever
newly won or lost), so a hybrid layer only earns its place if the numbers
say so — and any regression on precise questions is named, not hidden.

Usage:
    # embed the corpus first with the same embedder you'll query with
    python scripts/build_vectors.py --db data/uplink.db
    python scripts/eval_compare.py --db data/uplink.db --fixtures fixtures/<set>.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink.embeddings import get_embedder  # noqa: E402
from uplink.evaluate import run_eval  # noqa: E402
from uplink.hybrid import hybrid_search  # noqa: E402
from uplink.search import search  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/uplink.db")
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--collection", default=None)
    args = ap.parse_args()

    embedder = get_embedder()

    def bm25(db, q, k, collection):
        return search(db, q, k=k, collection=collection)

    def hybrid(db, q, k, collection):
        return hybrid_search(db, q, embedder, k=k, collection=collection)

    base = run_eval(args.db, args.fixtures, k=args.k,
                    collection=args.collection, retrieve_fn=bm25)
    hyb = run_eval(args.db, args.fixtures, k=args.k,
                   collection=args.collection, retrieve_fn=hybrid)

    def pct(n, d):
        return f"{n}/{d} ({n / d:.0%})" if d else "—"

    n = base.total
    print(f"embedder: {embedder.name}   questions: {n}\n")
    print(f"{'metric':<16}{'BM25':<20}{'hybrid':<20}{'delta'}")
    print(f"{'first correct':<16}{pct(base.hit_at_1, n):<20}"
          f"{pct(hyb.hit_at_1, n):<20}{hyb.hit_at_1 - base.hit_at_1:+d}")
    print(f"{'in top ' + str(args.k):<16}{pct(base.hit_at_k, n):<20}"
          f"{pct(hyb.hit_at_k, n):<20}{hyb.hit_at_k - base.hit_at_k:+d}")
    print(f"{'MRR':<16}{base.mrr:<20.3f}{hyb.mrr:<20.3f}{hyb.mrr - base.mrr:+.3f}")

    # Per-question diff: where did each retriever win or lose vs the other?
    by_q = {r["q"]: r["rank"] for r in base.per_question}
    print("\nper-question changes (rank; 0 = miss):")
    changed = False
    for r in hyb.per_question:
        b, h = by_q.get(r["q"], 0), r["rank"]
        if b != h:
            changed = True
            arrow = "won " if (h == 1 and b != 1) or (h and not b) else \
                    "lost" if (b == 1 and h != 1) or (b and not h) else "moved"
            print(f"  [{arrow}] BM25 rank {b} -> hybrid rank {h}   {r['q']}")
    if not changed:
        print("  (identical rankings on every question)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
