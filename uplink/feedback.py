"""Usage telemetry and the feedback → fixture loop.

Two append-only JSONL logs, both local files, both metadata-plus-corpus-paths
only (no document text beyond what the operator's own corpus contains):

    data/query-log.jsonl   every web search: ts, query, collection, hit
                           count, latency — the raw material for quality work
    data/feedback.jsonl    thumbs votes from the web UI: which result was
                           right (or wrong) for which query

`promote` turns thumbs-up votes into golden-question fixtures, so the eval
suite grows from real usage instead of hand-written guesses. Downvotes are
never promoted; they stay in the log as future hard-negative material.
"""

from __future__ import annotations

import json
from pathlib import Path

VALID_VOTES = ("up", "down")


def append_jsonl(path: Path, obj: dict) -> None:
    """Append one JSON line, creating parent folders on first use.

    Heals a torn tail first: a crash mid-write leaves a line with no
    newline, and appending onto it would fuse the two records and lose both.
    """
    from .notes import append_line

    append_line(path, json.dumps(obj, ensure_ascii=True))


def _norm(q: str) -> str:
    return " ".join(q.lower().split())


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # A torn write must not sink the whole log.
            continue


def promote(feedback_path: Path, out_path: Path) -> tuple[int, int]:
    """Promote thumbs-up votes into fixtures. Returns (added, skipped).

    Last vote wins: for each (question, path) pair the log is reduced to its
    FINAL vote before anything is promoted, so an upvote later retracted with
    a downvote never becomes a fixture. Idempotent: pairs already present in
    `out_path` are skipped, so re-running promote after new feedback only
    appends what is genuinely new. (A pair promoted in an earlier run is not
    retracted by a later downvote — remove it from the fixture file by hand.)
    """
    existing: set[tuple[str, str]] = set()
    for fx in _iter_jsonl(out_path):
        expects = fx.get("expect") or []
        if isinstance(expects, str):
            expects = [expects]
        for e in expects:
            existing.add((_norm(fx.get("q", "")), e))

    final: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    skipped = 0
    for vote in _iter_jsonl(feedback_path):
        q = (vote.get("q") or "").strip()
        path = (vote.get("path") or "").strip()
        if not q or not path or vote.get("vote") not in VALID_VOTES:
            skipped += 1
            continue
        key = (_norm(q), path)
        if key not in final:
            order.append(key)
        final[key] = {**vote, "q": q, "path": path}

    def expected(vote: dict) -> list[str]:
        """An upvoted ANSWER stood on every document it cited, so the fixture
        it becomes should accept any of them — not just the first."""
        paths = vote.get("paths")
        if isinstance(paths, list):
            clean = [str(p).strip() for p in paths if str(p or "").strip()]
            if clean:
                return list(dict.fromkeys(clean))
        return [vote["path"]]

    added = 0
    for key in order:
        vote = final[key]
        if vote["vote"] != "up" or key in existing:
            skipped += 1
            continue
        existing.add(key)
        append_jsonl(
            out_path,
            {
                "q": vote["q"],
                "expect": expected(vote),
                "source": "feedback",
                "kind": vote.get("kind", "hit"),
                "collection": vote.get("collection"),
                "ts": vote.get("ts"),
            },
        )
        added += 1
    return added, skipped
