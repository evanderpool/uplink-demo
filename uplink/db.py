"""SQLite storage layer for Uplink.

One database holds two things: a `documents` table (one row per source file,
with a content hash for incremental re-indexing) and a `chunks` table whose
text is mirrored into an FTS5 external-content index for BM25 ranking.

Since v0.2 every document belongs to a named `collection` (a department or
industry inside one organization). One database still belongs to one
organization — separate clients get separate database files, which keeps the
privacy boundary a filesystem boundary rather than a WHERE clause.

Search paths open the database in read-only mode (`mode=ro` URI) so the
retrieval layer is mechanically incapable of writing — a security property,
not a convention.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DEFAULT_COLLECTION = "main"

# Lowercase slug: safe in URLs, file paths, and shell commands unquoted.
_COLLECTION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    collection  TEXT NOT NULL DEFAULT 'main',
    path        TEXT NOT NULL,
    filetype    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    title       TEXT,
    indexed_at  TEXT NOT NULL,
    UNIQUE(collection, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES documents(id),
    seq      INTEGER NOT NULL,
    section  TEXT,
    text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- Phase-2 hybrid retrieval: one embedding vector per chunk, stored as packed
-- float32. Additive and optional — a database with no vectors simply runs
-- pure BM25, so this is safe to create everywhere. The embedder that produced
-- them is recorded in meta ('embedder') so a mismatched model is detected.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id  INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    vec       BLOB NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section)
    VALUES (new.id, new.text, new.section);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section)
    VALUES ('delete', old.id, old.text, old.section);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section)
    VALUES ('delete', old.id, old.text, old.section);
    INSERT INTO chunks_fts(rowid, text, section)
    VALUES (new.id, new.text, new.section);
END;
"""


def validate_collection(name: str) -> str:
    """Return `name` if it is a legal collection slug, else raise ValueError."""
    if not _COLLECTION_RE.match(name):
        raise ValueError(
            f"invalid collection name {name!r}: use 1-50 chars of "
            "lowercase letters, digits, '-' or '_', starting with a letter or digit"
        )
    return name


def connect_rw(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database for writing."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _migrate_v1_to_v2(conn)
    return conn


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Upgrade a pre-collections database in place.

    v1 `documents` had `path TEXT UNIQUE`; v2 scopes uniqueness to
    (collection, path), which requires a table rebuild. Existing rows land in
    the 'main' collection and the legacy `corpus_root` meta key becomes
    `corpus_root:main`. Chunk ids (and therefore the FTS index) are preserved.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if "collection" in cols:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        BEGIN;
        CREATE TABLE documents_v2 (
            id          INTEGER PRIMARY KEY,
            collection  TEXT NOT NULL DEFAULT 'main',
            path        TEXT NOT NULL,
            filetype    TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            mtime       REAL NOT NULL,
            size        INTEGER NOT NULL,
            title       TEXT,
            indexed_at  TEXT NOT NULL,
            UNIQUE(collection, path)
        );
        INSERT INTO documents_v2
            (id, collection, path, filetype, sha256, mtime, size, title, indexed_at)
        SELECT id, 'main', path, filetype, sha256, mtime, size, title, indexed_at
        FROM documents;
        DROP TABLE documents;
        ALTER TABLE documents_v2 RENAME TO documents;
        UPDATE OR REPLACE meta SET key = 'corpus_root:main' WHERE key = 'corpus_root';
        COMMIT;
        """
    )
    conn.execute("PRAGMA foreign_keys=ON")


def list_collections(conn: sqlite3.Connection) -> list[dict]:
    """Collections with document/chunk counts, alphabetical."""
    rows = conn.execute(
        """
        SELECT d.collection AS name,
               COUNT(DISTINCT d.id) AS documents,
               COUNT(c.id) AS chunks
        FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id
        GROUP BY d.collection ORDER BY d.collection
        """
    ).fetchall()
    return [dict(r) for r in rows]


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open the index database read-only. Raises if it does not exist."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Index database not found: {path}. Run `python -m uplink index <folder>` first."
        )
    # as_uri() percent-encodes '#', '%', and spaces — without it, a '#' in the
    # path silently drops '?mode=ro' and the "read-only" guarantee with it.
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    except sqlite3.Error:
        conn.close()  # corrupt / not-a-database file: don't leak the handle
        raise
    if cols and "collection" not in cols:
        conn.close()
        raise ValueError(
            f"{path} is a v0.1 index (no collections). Upgrade it once with "
            f"`python -m uplink upgrade --db {path}` — contents are preserved."
        )
    return conn
