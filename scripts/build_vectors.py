"""Embed the indexed corpus for hybrid retrieval.

Run after building the text index. The embedder follows UPLINK_EMBEDDER
(local | voyage | openai | hash); pick the one matching the deployment's
trust posture — local/hash keep everything on the machine, voyage/openai
send chunk text to that provider.

    python scripts/build_vectors.py --db data/uplink.db          # incremental
    python scripts/build_vectors.py --db data/uplink.db --rebuild
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink.embeddings import get_embedder  # noqa: E402
from uplink.vectors import build_vectors  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/uplink.db")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-embed every chunk (use after switching embedder)")
    args = ap.parse_args()
    embedder = get_embedder()
    n = build_vectors(args.db, embedder, rebuild=args.rebuild)
    print(f"embedded {n} chunks with {embedder.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
