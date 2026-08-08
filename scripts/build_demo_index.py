"""Build the demo's index at deploy time.

The demo's documents live in demo-docs/<collection>/ and ship with the
repo — all public records (SEC filings), so committing them is fine and
every deploy is fast and identical. Run from the repo root:

    python scripts/build_demo_index.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink.indexer import index_folder  # noqa: E402

DB = Path("data") / "uplink.db"
CORPORA = Path("demo-docs")


def discard_fake_pdfs() -> None:
    """Drop downloads that aren't what their name claims.

    Some sources (CDC especially) answer datacenter IPs with an HTML error
    page instead of the file. A .pdf that doesn't start with %PDF is one of
    those — indexing it can only fail, so discard it and say so. The demo
    ships with the documents that fetched cleanly.
    """
    for pdf in CORPORA.rglob("*.pdf"):
        try:
            head = pdf.read_bytes()[:5]
        except OSError:
            continue
        if head != b"%PDF-":
            print(f"discarded {pdf.relative_to(CORPORA)} — the server sent "
                  "an error page instead of the PDF")
            pdf.unlink()


def main() -> int:
    DB.parent.mkdir(parents=True, exist_ok=True)
    industries = [p for p in sorted(CORPORA.iterdir()) if p.is_dir()]
    if not industries:
        print("error: no documents found under demo-docs/", file=sys.stderr)
        return 2
    discard_fake_pdfs()
    total = 0
    for folder in industries:
        stats = index_folder(folder, DB, collection=folder.name)
        total += stats.indexed
        print(f"[{folder.name}] indexed {stats.indexed}, chunks {stats.chunks}, "
              f"errors {len(stats.errors)}")
        for err in stats.errors:
            # A skipped file is a smaller demo, not a broken one.
            print(f"  skipped: {err}", file=sys.stderr)
    if total == 0:
        print("error: nothing indexed — the demo would be empty", file=sys.stderr)
        return 1
    print(f"demo index ready: {total} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
