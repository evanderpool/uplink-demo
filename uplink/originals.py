"""Serving the original source file behind a citation.

The indexed text proves what retrieval saw; the original proves what the
document actually is. Both matter, so the reader offers both.

Security: this is the one place Uplink reads a corpus file at request time,
so the path is never taken from the client. The client names a document by
(collection, path); that pair is looked up in the index, joined to the
collection's own recorded corpus root, resolved, and then REQUIRED to still
be inside that root. A path that is not in the index cannot be requested at
all, and one that escapes its root after resolution (symlink, junction, or
a stale row) is refused rather than served.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Only types the indexer itself accepts. `inline` is safe for these: the
# browser either renders them or downloads them, and nosniff prevents a
# mislabelled file from being interpreted as something else.
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".markdown": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".tsv": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}

# Types a browser renders in place. Everything else is offered as a download
# while the indexed-text view stays the readable option.
VIEWABLE = {".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv"}

MAX_FILE_BYTES = 80 * 1024 * 1024


class OriginalUnavailable(Exception):
    """The original file cannot be served — with a reason worth showing."""


def resolve_original(
    conn: sqlite3.Connection, collection: str, path: str
) -> tuple[Path, str]:
    """Return (file, content_type) for an indexed document, or raise.

    `conn` must be a read-only connection to the index.
    """
    row = conn.execute(
        "SELECT path, filetype FROM documents WHERE collection = ? AND path = ?",
        (collection, path),
    ).fetchone()
    if row is None:
        raise OriginalUnavailable("document is not in the index")

    meta = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (f"corpus_root:{collection}",)
    ).fetchone()
    if meta is None:
        raise OriginalUnavailable("this collection has no recorded source folder")

    root = Path(meta["value"]).resolve()
    # row["path"] is the stored relative path, never client input.
    candidate = (root / row["path"]).resolve()
    if not candidate.is_relative_to(root):
        # A symlink or junction pointing outside the corpus, or a poisoned
        # row: refuse rather than read.
        raise OriginalUnavailable("resolved outside the collection's source folder")
    if not candidate.is_file():
        raise OriginalUnavailable("the original file is no longer on disk")

    suffix = candidate.suffix.lower()
    ctype = CONTENT_TYPES.get(suffix)
    if ctype is None:
        raise OriginalUnavailable(f"unsupported file type '{suffix}'")
    if candidate.stat().st_size > MAX_FILE_BYTES:
        raise OriginalUnavailable("the original file is too large to serve")
    return candidate, ctype


def is_viewable(path_or_name: str) -> bool:
    return Path(path_or_name).suffix.lower() in VIEWABLE
