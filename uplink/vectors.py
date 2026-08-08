"""Chunk vector storage and brute-force semantic search.

Deliberately dependency-light: vectors are packed float32 in a SQLite BLOB
column, and search is an exact cosine scan in Python. At this corpus's scale
(hundreds to low thousands of chunks) an exact scan is instant and keeps the
"copy the folder, it runs" property — no vector database, no ANN index to
build. For a much larger corpus this O(n) scan is the thing to replace with a
real ANN index; it is not the shape of the algorithm that would change, only
the lookup.
"""

from __future__ import annotations

import sqlite3
from array import array

from . import db
from .search import Hit


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def build_vectors(db_path, embedder, batch: int = 64, rebuild: bool = False) -> int:
    """Embed every chunk that lacks a vector; return how many were embedded.

    Idempotent: re-running only embeds new chunks unless `rebuild` is set.
    The embedder's name is stamped into meta so a later search can refuse a
    query embedded by a different model.
    """
    conn = db.connect_rw(db_path)
    try:
        if rebuild:
            conn.execute("DELETE FROM chunk_vectors")
        rows = conn.execute(
            """SELECT c.id, c.text FROM chunks c
               LEFT JOIN chunk_vectors v ON v.chunk_id = c.id
               WHERE v.chunk_id IS NULL ORDER BY c.id"""
        ).fetchall()
        done = 0
        for i in range(0, len(rows), batch):
            window = rows[i:i + batch]
            vecs = embedder.embed([r["text"] for r in window])
            conn.executemany(
                "INSERT OR REPLACE INTO chunk_vectors(chunk_id, vec) VALUES (?, ?)",
                [(r["id"], _pack(v)) for r, v in zip(window, vecs)],
            )
            done += len(window)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('embedder', ?)",
            (embedder.name,),
        )
        conn.commit()
        return done
    finally:
        conn.close()


def embedder_matches(db_path, name: str) -> bool:
    """True only if this database has vectors AND they were built by the
    embedder named `name`.

    Cosine across different models — or different dimensions — is
    meaningless, so the query embedder MUST match the one that built the
    stored vectors. Callers use this to decide whether the vector side is
    safe to run; a False (no vectors, or a different model) means fall back
    to pure BM25 rather than fuse garbage. This is the guard the schema's
    meta('embedder') stamp exists for."""
    try:
        conn = db.connect_ro(db_path)
    except Exception:
        return False
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'embedder'").fetchone()
        return row is not None and row["value"] == name
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _filter_sql(collection, include, exclude):
    where, params = [], []
    if collection is not None:
        where.append("d.collection = ?")
        params.append(collection)
    if include is not None:
        if not include:
            return None, None  # everything deselected
        where.append("(d.collection, d.path) IN (VALUES "
                     + ",".join(["(?,?)"] * len(include)) + ")")
        for c, p in include:
            params += [c, p]
    if exclude:
        where.append("NOT (d.collection, d.path) IN (VALUES "
                     + ",".join(["(?,?)"] * len(exclude)) + ")")
        for c, p in exclude:
            params += [c, p]
    return (" AND ".join(where) if where else "1"), params


def vector_search(db_path, query_vec, k=8, max_text=1200, collection=None,
                  include=None, exclude=None) -> list[Hit]:
    """Exact cosine nearest-neighbour search over stored chunk vectors.

    Query and stored vectors are unit-normalised, so cosine reduces to a dot
    product. Applies the same collection/scope filters as BM25 so a scoped
    question never sees deselected documents.
    """
    conn = db.connect_ro(db_path)
    try:
        clause, params = _filter_sql(collection, include, exclude)
        if clause is None:
            return []
        rows = conn.execute(
            f"""SELECT v.chunk_id AS cid, v.vec AS vec,
                       d.path, d.title, d.filetype, d.collection,
                       c.section, c.seq, c.text
                FROM chunk_vectors v
                JOIN chunks c ON c.id = v.chunk_id
                JOIN documents d ON d.id = c.doc_id
                WHERE {clause}""",
            params,
        ).fetchall()
        q = array("f", query_vec)
        # Dimension guard: zip() would silently truncate a mismatched query
        # to the shorter length and return a meaningless-but-sortable number.
        # Refuse instead, so the caller degrades to BM25.
        if rows and len(_unpack(rows[0]["vec"])) != len(q):
            raise ValueError("query vector dimension does not match the index")
        scored = []
        for r in rows:
            vec = _unpack(r["vec"])
            dot = sum(a * b for a, b in zip(q, vec))
            scored.append((dot, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        hits = []
        for dot, r in scored[:k]:
            text = r["text"]
            hits.append(Hit(
                path=r["path"], title=r["title"], filetype=r["filetype"],
                collection=r["collection"], section=r["section"] or "",
                seq=r["seq"], score=round(float(dot), 4), snippet="",
                text=text[:max_text] + ("..." if len(text) > max_text else ""),
            ))
        return hits
    finally:
        conn.close()
