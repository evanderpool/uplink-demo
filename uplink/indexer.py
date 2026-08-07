"""Walk a corpus folder and build/refresh the search index.

Incremental: a file with unchanged mtime+size is skipped without hashing;
if either differs, the SHA-256 decides whether re-chunking is needed. Deleted
files are purged. Each collection belongs to one corpus root — indexing a
different root into an existing collection is refused rather than silently
purging its documents.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .chunker import chunk_section
from .extractors import SUPPORTED_EXTENSIONS, ExtractorUnavailable, extract

IGNORED_DIRS = {
    ".git", ".uplink", "__pycache__", "node_modules",
    ".venv", "venv", ".idea", ".vscode",
}


class CorpusMismatch(ValueError):
    """The collection already belongs to a different corpus root."""


@dataclass
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"scanned:   {self.scanned}",
            f"indexed:   {self.indexed}",
            f"unchanged: {self.unchanged}",
            f"removed:   {self.removed}",
            f"chunks:    {self.chunks}",
        ]
        for w in self.warnings:
            lines.append(f"warning:   {w}")
        for e in self.errors:
            lines.append(f"error:     {e}")
        return "\n".join(lines)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _iter_files(root: Path, skip: set[Path]) -> list[Path]:
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in IGNORED_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        if p.resolve() in skip:
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)
    return files


def _check_corpus_root(conn: sqlite3.Connection, root: Path, collection: str) -> None:
    key = f"corpus_root:{collection}"
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row and row["value"] != str(root):
        raise CorpusMismatch(
            f"Collection '{collection}' indexes '{row['value']}'. Refusing to "
            f"index '{root}' into it (that would purge the existing documents). "
            f"Use a different --collection, or a separate --db file."
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(root))
    )


def index_folder(
    corpus_dir: str | Path,
    db_path: str | Path,
    collection: str = db.DEFAULT_COLLECTION,
) -> IndexStats:
    collection = db.validate_collection(collection)
    root = Path(corpus_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Corpus folder not found: {root}")

    # Never index the database (or its WAL/SHM sidecars) into itself.
    db_file = Path(db_path).resolve()
    skip = {db_file, db_file.with_name(db_file.name + "-wal"),
            db_file.with_name(db_file.name + "-shm")}

    stats = IndexStats()
    conn = db.connect_rw(db_path)
    try:
        _check_corpus_root(conn, root, collection)
        seen_paths: set[str] = set()
        for path in _iter_files(root, skip):
            rel = path.relative_to(root).as_posix()
            seen_paths.add(rel)
            stats.scanned += 1
            try:
                _index_file(conn, collection, rel, path, stats)
            except ExtractorUnavailable as exc:
                stats.errors.append(f"{rel}: {exc}")
            except Exception as exc:  # one bad file must not sink the run
                stats.errors.append(f"{rel}: {type(exc).__name__}: {exc}")

        # Purge documents (in this collection only) whose files no longer exist.
        for row in conn.execute(
            "SELECT id, path FROM documents WHERE collection = ?", (collection,)
        ).fetchall():
            if row["path"] not in seen_paths:
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (row["id"],))
                conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
                stats.removed += 1

        conn.commit()
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        conn.commit()
    finally:
        conn.close()
    return stats


def forget(db_path: str | Path, collection: str | None = None) -> dict:
    """Remove a collection from the index — or every collection.

    The counterpart to `index_folder`, which had none: a collection could be
    created and refreshed but never dropped, so clearing one meant editing
    the database by hand. Source files on disk are never touched; this
    removes only what was indexed, along with the collection's recorded
    corpus root so the name is genuinely free for reuse afterwards.
    """
    conn = db.connect_rw(db_path)
    try:
        if collection is not None:
            collection = db.validate_collection(collection)
            rows = conn.execute(
                "SELECT id FROM documents WHERE collection = ?", (collection,)
            ).fetchall()
            doc_ids = [r["id"] for r in rows]
            chunks = conn.execute(
                "SELECT COUNT(*) n FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE d.collection = ?", (collection,)
            ).fetchone()["n"]
            for doc_id in doc_ids:
                conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE collection = ?", (collection,))
            conn.execute("DELETE FROM meta WHERE key = ?", (f"corpus_root:{collection}",))
            removed = {"collections": [collection] if doc_ids else [],
                       "documents": len(doc_ids), "chunks": chunks}
        else:
            names = [r["collection"] for r in conn.execute(
                "SELECT DISTINCT collection FROM documents ORDER BY collection")]
            docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
            chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM documents")
            conn.execute("DELETE FROM meta WHERE key LIKE 'corpus_root:%'")
            removed = {"collections": names, "documents": docs, "chunks": chunks}

        conn.commit()
        # Rebuild the FTS index rather than leave tombstones behind, so a
        # cleared database is genuinely empty and not merely hidden.
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return removed


def _index_file(
    conn: sqlite3.Connection, collection: str, rel: str, path: Path, stats: IndexStats
) -> None:
    stat = path.stat()
    existing = conn.execute(
        "SELECT id, sha256, mtime, size FROM documents "
        "WHERE collection = ? AND path = ?",
        (collection, rel),
    ).fetchone()

    # Fast path: identical mtime+size means unchanged — skip hashing entirely.
    if existing and existing["mtime"] == stat.st_mtime and existing["size"] == stat.st_size:
        stats.unchanged += 1
        return

    sha = _sha256(path)
    if existing and existing["sha256"] == sha:
        # Content identical; the file was merely touched. Refresh metadata.
        conn.execute(
            "UPDATE documents SET mtime = ?, size = ? WHERE id = ?",
            (stat.st_mtime, stat.st_size, existing["id"]),
        )
        stats.unchanged += 1
        return

    extracted = extract(path)
    for w in extracted.warnings:
        stats.warnings.append(f"{rel}: {w}")

    if existing:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (existing["id"],))
        conn.execute("DELETE FROM documents WHERE id = ?", (existing["id"],))

    cur = conn.execute(
        "INSERT INTO documents"
        "(collection, path, filetype, sha256, mtime, size, title, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            collection,
            rel,
            path.suffix.lower().lstrip("."),
            sha,
            stat.st_mtime,
            stat.st_size,
            extracted.title or path.stem,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    doc_id = cur.lastrowid

    seq = 0
    for section in extracted.sections:
        for chunk in chunk_section(section.text, header=section.header):
            conn.execute(
                "INSERT INTO chunks(doc_id, seq, section, text) VALUES (?, ?, ?, ?)",
                (doc_id, seq, section.title, chunk),
            )
            seq += 1
            stats.chunks += 1
    stats.indexed += 1
