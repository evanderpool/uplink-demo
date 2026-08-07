"""Export the index as JSONL — one line per document, chunks inline.

Read-only by construction (mode=ro connection). This is the migration path
out of Uplink: the JSONL shape maps 1:1 onto a Postgres/Supabase schema for
the eventual vector-store phase, and it doubles as a human-auditable dump of
exactly what the index holds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from . import db


def export_jsonl(
    db_path: str | Path, out: TextIO, collection: str | None = None
) -> int:
    """Write one JSON line per document to `out`; returns the document count."""
    conn = db.connect_ro(db_path)
    try:
        where = ""
        params: list = []
        if collection is not None:
            where = "WHERE collection = ?"
            params.append(db.validate_collection(collection))
        docs = conn.execute(
            f"""
            SELECT id, collection, path, filetype, sha256, title, indexed_at
            FROM documents {where} ORDER BY collection, path
            """,
            params,
        ).fetchall()
        for doc in docs:
            chunks = conn.execute(
                "SELECT seq, section, text FROM chunks WHERE doc_id = ? ORDER BY seq",
                (doc["id"],),
            ).fetchall()
            line = {
                "collection": doc["collection"],
                "path": doc["path"],
                "title": doc["title"],
                "filetype": doc["filetype"],
                "sha256": doc["sha256"],
                "indexed_at": doc["indexed_at"],
                "chunks": [
                    {"seq": c["seq"], "section": c["section"], "text": c["text"]}
                    for c in chunks
                ],
            }
            out.write(json.dumps(line, ensure_ascii=True) + "\n")
        return len(docs)
    finally:
        conn.close()
