"""Saved notes — the durable artifacts a session leaves behind.

An answer you cannot keep is a session, not a workspace. Notes are stored as
one JSONL file next to the database (`data/notes.jsonl`), append-only with
tombstones rather than rewrites, so a crash mid-write can never destroy the
notes that came before.

A note carries the question, the answer text, and the citations that
grounded it, so a saved answer stays verifiable — clicking its citations
still opens the indexed passages.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

NOTE_ID_RE = re.compile(r"^[a-f0-9]{12}\Z")
MAX_NOTE_CHARS = 20_000
MAX_CITATIONS = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_line(path: Path, line: str) -> None:
    """Append one JSONL record, healing a torn tail first.

    A crash mid-write leaves a line with no newline; appending straight onto
    it would fuse the two records and lose BOTH — the torn one and the new
    one. Terminating the file first costs one stat and keeps every later
    record readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            needs_newline = fh.read(1) != b"\n"
        if needs_newline:
            with path.open("a", encoding="utf-8") as fh:
                fh.write("\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _clean_citations(raw) -> list[dict]:
    """Keep only well-formed citations, so a saved note stays clickable."""
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw[:MAX_CITATIONS]:
        if not isinstance(c, dict):
            continue
        path = str(c.get("path") or "").strip()[:500]
        if not path:
            continue
        entry = {"path": path, "section": str(c.get("section") or "")[:300]}
        if isinstance(c.get("seq"), int):
            entry["seq"] = c["seq"]
        if c.get("collection"):
            entry["collection"] = str(c["collection"])[:60]
        out.append(entry)
    return out


def add_note(
    notes_path: Path,
    title: str,
    body: str,
    citations=None,
    collection: str | None = None,
    kind: str = "answer",
) -> dict:
    note = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now(),
        "kind": kind if kind in ("answer", "manual") else "answer",
        "title": title.strip()[:300],
        "body": body.strip()[:MAX_NOTE_CHARS],
        "citations": _clean_citations(citations),
        "collection": collection or None,
        "deleted": False,
    }
    append_line(notes_path, json.dumps(note, ensure_ascii=True))
    return note


def delete_note(notes_path: Path, note_id: str) -> bool:
    """Append a tombstone. Returns False when the id is unknown."""
    if not NOTE_ID_RE.match(note_id):
        raise ValueError("invalid note id")
    if not any(n["id"] == note_id for n in list_notes(notes_path)):
        return False
    append_line(notes_path, json.dumps({"id": note_id, "deleted": True, "ts": _now()}))
    return True


def list_notes(notes_path: Path, collection: str | None = None) -> list[dict]:
    """Live notes, newest first. Torn lines are skipped, never fatal."""
    if not notes_path.exists():
        return []
    by_id: dict[str, dict] = {}
    for line in notes_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not NOTE_ID_RE.match(str(row.get("id", ""))):
            continue
        if row.get("deleted"):
            by_id.pop(row["id"], None)
            continue
        by_id[row["id"]] = row
    notes = list(by_id.values())
    if collection:
        notes = [n for n in notes if n.get("collection") == collection]
    notes.sort(key=lambda n: n.get("ts", ""), reverse=True)
    return notes
