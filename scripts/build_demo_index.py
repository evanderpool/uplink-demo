"""Build the demo's index at deploy time.

The demo repo ships no documents and no database — corpora are public
sources fetched by scripts/fetch_corpora.py during the build, then indexed
here. Run from the repo root:

    python scripts/fetch_corpora.py
    python scripts/build_demo_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink.indexer import index_folder  # noqa: E402

DB = Path("data") / "uplink.db"
CORPORA = Path("corpora")


def main() -> int:
    DB.parent.mkdir(parents=True, exist_ok=True)
    industries = [p for p in sorted(CORPORA.iterdir()) if p.is_dir()]
    if not industries:
        print("error: no corpora found — run scripts/fetch_corpora.py first",
              file=sys.stderr)
        return 2
    failed = False
    for folder in industries:
        stats = index_folder(folder, DB, collection=folder.name)
        print(f"[{folder.name}] indexed {stats.indexed}, chunks {stats.chunks}, "
              f"errors {len(stats.errors)}")
        for err in stats.errors:
            print(f"  error: {err}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
