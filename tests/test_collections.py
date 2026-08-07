"""Collections: schema v2, the v1 migration, scoped purges, export, promote."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from uplink import db
from uplink.export import export_jsonl
from uplink.feedback import append_jsonl, promote
from uplink.indexer import CorpusMismatch, index_folder
from uplink.search import search

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, filetype TEXT NOT NULL,
    sha256 TEXT NOT NULL, mtime REAL NOT NULL, size INTEGER NOT NULL,
    title TEXT, indexed_at TEXT NOT NULL
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id),
    seq INTEGER NOT NULL, section TEXT, text TEXT NOT NULL
);
CREATE INDEX idx_chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, section, content='chunks', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section)
    VALUES (new.id, new.text, new.section);
END;
"""


def _folder(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    root = tmp_path / name
    root.mkdir()
    for fname, text in files.items():
        (root / fname).write_text(text, encoding="utf-8")
    return root


def test_two_collections_index_and_filter(tmp_path: Path):
    ops = _folder(tmp_path, "ops", {"backup.md": "# Backups\n\nBackups run nightly."})
    fin = _folder(tmp_path, "fin", {"budget.md": "# Budget\n\nThe budget renews in April."})
    dbp = tmp_path / "u.db"
    index_folder(ops, dbp, collection="ops")
    index_folder(fin, dbp, collection="finance")

    all_hits = search(dbp, "nightly backups budget April", k=10)
    assert {h.collection for h in all_hits} == {"ops", "finance"}

    only_fin = search(dbp, "budget April renews", k=10, collection="finance")
    assert only_fin and all(h.collection == "finance" for h in only_fin)
    assert search(dbp, "nightly backups", k=10, collection="finance") == []


def test_same_relative_path_in_two_collections(tmp_path: Path):
    a = _folder(tmp_path, "a", {"readme.md": "# A\n\nAlpha team charter."})
    b = _folder(tmp_path, "b", {"readme.md": "# B\n\nBravo team charter."})
    dbp = tmp_path / "u.db"
    index_folder(a, dbp, collection="alpha")
    index_folder(b, dbp, collection="bravo")
    hits = search(dbp, "team charter", k=10)
    assert {(h.collection, h.path) for h in hits} == {
        ("alpha", "readme.md"), ("bravo", "readme.md"),
    }


def test_purge_is_scoped_to_collection(tmp_path: Path):
    ops = _folder(tmp_path, "ops", {"backup.md": "# Backups\n\nBackups run nightly."})
    fin = _folder(tmp_path, "fin", {"budget.md": "# Budget\n\nThe budget renews in April."})
    dbp = tmp_path / "u.db"
    index_folder(ops, dbp, collection="ops")
    index_folder(fin, dbp, collection="finance")

    (ops / "backup.md").unlink()
    stats = index_folder(ops, dbp, collection="ops")
    assert stats.removed == 1
    # Finance must be untouched by the ops purge.
    assert search(dbp, "budget April renews", k=5, collection="finance")


def test_corpus_root_scoped_per_collection(tmp_path: Path):
    a = _folder(tmp_path, "a", {"x.md": "# X\n\nxray"})
    b = _folder(tmp_path, "b", {"y.md": "# Y\n\nyankee"})
    dbp = tmp_path / "u.db"
    index_folder(a, dbp, collection="one")
    with pytest.raises(CorpusMismatch):
        index_folder(b, dbp, collection="one")
    index_folder(b, dbp, collection="two")  # different collection: fine


@pytest.mark.parametrize("bad", ["../evil", "UPPER", "sp ace", "", "a" * 51, "-lead"])
def test_invalid_collection_names_rejected(tmp_path: Path, bad: str):
    a = _folder(tmp_path, "a" + str(len(bad)), {"x.md": "# X\n\nxray"})
    with pytest.raises(ValueError):
        index_folder(a, tmp_path / "u.db", collection=bad)


def test_v1_database_migrates_in_place(tmp_path: Path):
    dbp = tmp_path / "old.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO documents(id, path, filetype, sha256, mtime, size, title, indexed_at) "
        "VALUES (1, 'legacy.md', 'md', 'abc', 0, 10, 'Legacy', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO chunks(id, doc_id, seq, section, text) "
        "VALUES (1, 1, 0, 'Legacy', 'The legacy escalation policy pages the on-call.')"
    )
    conn.execute("INSERT INTO meta(key, value) VALUES ('corpus_root', '/old/root')")
    conn.commit()
    conn.close()

    # Opening for write migrates; rows land in 'main' and search still works.
    db.connect_rw(dbp).close()
    hits = search(dbp, "legacy escalation policy", k=5)
    assert hits and hits[0].collection == "main" and hits[0].path == "legacy.md"
    assert search(dbp, "legacy escalation policy", k=5, collection="main")

    ro = db.connect_ro(dbp)
    try:
        row = ro.execute("SELECT value FROM meta WHERE key='corpus_root:main'").fetchone()
        assert row["value"] == "/old/root"
        assert ro.execute("SELECT COUNT(*) n FROM meta WHERE key='corpus_root'").fetchone()["n"] == 0
    finally:
        ro.close()

    # Migration is idempotent.
    db.connect_rw(dbp).close()


def test_v1_database_read_only_gives_upgrade_message(tmp_path: Path):
    dbp = tmp_path / "old.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript(V1_SCHEMA)
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="upgrade"):
        db.connect_ro(dbp)


def test_list_collections_counts(tmp_path: Path):
    a = _folder(tmp_path, "a", {"x.md": "# X\n\nxray", "z.md": "# Z\n\nzulu"})
    dbp = tmp_path / "u.db"
    index_folder(a, dbp, collection="ops")
    conn = db.connect_ro(dbp)
    try:
        cols = db.list_collections(conn)
    finally:
        conn.close()
    assert len(cols) == 1
    assert cols[0]["name"] == "ops" and cols[0]["documents"] == 2


# ------------------------------------------------------------------ export

def test_export_jsonl_shape_and_filter(tmp_path: Path):
    a = _folder(tmp_path, "a", {"x.md": "# X\n\nxray tango"})
    b = _folder(tmp_path, "b", {"y.md": "# Y\n\nyankee foxtrot"})
    dbp = tmp_path / "u.db"
    index_folder(a, dbp, collection="alpha")
    index_folder(b, dbp, collection="bravo")

    buf = io.StringIO()
    n = export_jsonl(dbp, buf, collection=None)
    assert n == 2
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert {ln["collection"] for ln in lines} == {"alpha", "bravo"}
    doc = next(ln for ln in lines if ln["collection"] == "alpha")
    assert doc["path"] == "x.md" and doc["chunks"]
    assert "xray" in doc["chunks"][0]["text"]
    assert doc["sha256"] and doc["indexed_at"]

    buf = io.StringIO()
    assert export_jsonl(dbp, buf, collection="bravo") == 1


# ----------------------------------------------------------------- promote

def test_promote_dedupes_and_skips_downvotes(tmp_path: Path):
    fb = tmp_path / "feedback.jsonl"
    out = tmp_path / "promoted.jsonl"
    votes = [
        {"ts": "t1", "q": "How do backups run?", "path": "backup.md", "vote": "up"},
        {"ts": "t2", "q": "how do backups  run?", "path": "backup.md", "vote": "up"},  # dup (normalized)
        {"ts": "t3", "q": "budget renewal", "path": "budget.md", "vote": "down"},     # downvote
        {"ts": "t4", "q": "budget renewal", "path": "budget.md", "vote": "up"},
    ]
    for v in votes:
        append_jsonl(fb, v)

    added, _ = promote(fb, out)
    assert added == 2
    fixtures = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    # Last vote wins, so the dup's (latest) phrasing is the one promoted.
    assert [fx["q"] for fx in fixtures] == ["how do backups  run?", "budget renewal"]
    assert fixtures[0]["expect"] == ["backup.md"]

    # Re-running promotes nothing new.
    added, _ = promote(fb, out)
    assert added == 0


def test_promote_last_vote_wins(tmp_path: Path):
    """An upvote later retracted with a downvote is never promoted."""
    fb = tmp_path / "feedback.jsonl"
    out = tmp_path / "promoted.jsonl"
    append_jsonl(fb, {"q": "refund policy", "path": "refunds.md", "vote": "up"})
    append_jsonl(fb, {"q": "refund policy", "path": "refunds.md", "vote": "down"})
    added, _ = promote(fb, out)
    assert added == 0
    assert not out.exists()
    # And a downvote later corrected to an upvote IS promoted.
    append_jsonl(fb, {"q": "refund policy", "path": "refunds.md", "vote": "up"})
    added, _ = promote(fb, out)
    assert added == 1


def test_quality_history_filtered_by_provenance(tmp_path: Path):
    """Runs against other corpora/fixture sets must not chart as one line."""
    from uplink.report import quality_data

    a = _folder(tmp_path, "a", {"x.md": "# X\n\nxray tango"})
    dbp = tmp_path / "uplink.db"
    index_folder(a, dbp, collection="main")
    fx = tmp_path / "golden.jsonl"
    fx.write_text('{"q": "xray tango", "expect": ["x.md"]}\n', encoding="utf-8")

    base = {"ts": "t", "questions": 1, "hit_at_1": 1, "hit_at_k": 1, "k": 5, "mrr": 1.0}
    rows = [
        {**base, "db": "uplink.db", "fixtures": "golden.jsonl"},   # ours
        {**base, "db": "other.db", "fixtures": "golden.jsonl"},    # other corpus
        {**base, "db": "uplink.db", "fixtures": "industry.jsonl"}, # other fixtures
        base,                                                      # legacy, no provenance
    ]
    hist = tmp_path / "hist.jsonl"
    hist.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    data = quality_data(dbp, fx, hist)
    assert len(data["history"]) == 1


def test_promote_survives_torn_log_line(tmp_path: Path):
    fb = tmp_path / "feedback.jsonl"
    fb.write_text(
        '{"q": "good question", "path": "a.md", "vote": "up"}\n{"q": "torn', encoding="utf-8"
    )
    added, _ = promote(fb, tmp_path / "out.jsonl")
    assert added == 1


# ---------------------------------------- pins from the v0.2 code review

def test_eval_history_survives_torn_line(tmp_path: Path):
    """One crash-torn line must not kill every future quality report."""
    from uplink.report import load_history

    hist = tmp_path / "hist.jsonl"
    hist.write_text(
        '{"ts": "t", "questions": 1, "hit_at_1": 1, "hit_at_k": 1, "k": 5, "mrr": 1.0}\n'
        '{"ts": "t2", "questio',
        encoding="utf-8",
    )
    rows = load_history(hist)
    assert len(rows) == 1 and rows[0]["mrr"] == 1.0


def test_cli_clean_error_on_corrupt_db(tmp_path: Path, capsys):
    """A non-database --db file gets `error: ...` and exit 2, not a traceback."""
    from uplink.__main__ import main

    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"this is not sqlite")
    rc = main(["search", "hello", "--db", str(bad)])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_cli_promote_missing_feedback_errors(tmp_path: Path, capsys):
    """A typo'd --feedback path must not report success as '0 promoted'."""
    from uplink.__main__ import main

    rc = main([
        "promote",
        "--feedback", str(tmp_path / "nope.jsonl"),
        "--out", str(tmp_path / "out.jsonl"),
    ])
    assert rc == 2
    assert "not found" in capsys.readouterr().err.lower()
