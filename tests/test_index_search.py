"""End-to-end: index the mixed corpus, search it, evaluate it, stay read-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from uplink import db
from uplink.evaluate import run_eval
from uplink.indexer import index_folder
from uplink.search import build_match, search


@pytest.fixture
def indexed(corpus: Path, tmp_path: Path):
    db_path = tmp_path / "index.db"
    stats = index_folder(corpus, db_path)
    return corpus, db_path, stats


def test_index_counts(indexed):
    _, _, stats = indexed
    assert stats.indexed == 8  # md, txt, csv, tsv, pdf, docx, xlsx, xls
    assert stats.errors == []
    assert stats.chunks >= 8


def test_ignored_files_not_indexed(indexed):
    _, db_path, _ = indexed
    conn = db.connect_ro(db_path)
    paths = [r["path"] for r in conn.execute("SELECT path FROM documents")]
    conn.close()
    assert not any("png" in p for p in paths)
    assert not any(".git" in p for p in paths)


@pytest.mark.parametrize(
    ("query", "expect_path"),
    [
        ("when do backups run", "handbook.md"),
        ("quarterly maintenance window", "notes.txt"),
        ("who owns firewall-01", "inventory.csv"),
        ("meaning of code E42", "codes.tsv"),
        ("when does the migration freeze end", "briefing.pdf"),
        ("badge request approval", "procedure.docx"),
        ("tailscale subscription cost", "budget.xlsx"),
        ("acme telecom contract renewal", "vendors.xls"),
    ],
)
def test_search_finds_every_file_type(indexed, query, expect_path):
    _, db_path, _ = indexed
    hits = search(db_path, query, k=5)
    assert hits, f"no results for: {query}"
    assert any(h.path.endswith(expect_path) for h in hits), (
        f"{expect_path} not in top-5 for '{query}': {[h.path for h in hits]}"
    )


def test_incremental_reindex_skips_unchanged(indexed):
    corpus, db_path, _ = indexed
    stats2 = index_folder(corpus, db_path)
    assert stats2.indexed == 0
    assert stats2.unchanged == 8


def test_changed_file_reindexed_and_old_chunks_gone(indexed):
    corpus, db_path, _ = indexed
    (corpus / "notes.txt").write_text(
        "The maintenance window moved to the third Sunday.", encoding="utf-8"
    )
    stats = index_folder(corpus, db_path)
    assert stats.indexed == 1
    hits = search(db_path, "maintenance window third Sunday", k=3)
    assert hits and hits[0].path.endswith("notes.txt")
    stale = search(db_path, "second Saturday maintenance", k=5)
    assert not any("second Saturday" in h.text for h in stale)


def test_deleted_file_purged(indexed):
    corpus, db_path, _ = indexed
    (corpus / "codes.tsv").unlink()
    stats = index_folder(corpus, db_path)
    assert stats.removed == 1
    assert not search(db_path, "disk failure imminent", k=5)


def test_search_connection_is_read_only(indexed):
    _, db_path, _ = indexed
    conn = db.connect_ro(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO documents(path, filetype, sha256, mtime, indexed_at) "
                     "VALUES ('x', 'md', 'x', 0, 'now')")
    conn.close()


def test_fts_query_injection_is_neutralised(indexed):
    _, db_path, _ = indexed
    # FTS5 operators and syntax in user input must not raise.
    for hostile in ['"unclosed', "NEAR(a b)", "a* AND -b", "col:val OR (", "*"]:
        search(db_path, hostile, k=3)  # must not raise


def test_build_match_quotes_tokens():
    assert build_match("drop table docs") == '"drop" "table" "docs"'
    assert build_match("!!!") is None


def test_eval_harness(indexed, tmp_path: Path):
    _, db_path, _ = indexed
    fixtures = tmp_path / "golden.jsonl"
    fixtures.write_text(
        json.dumps({"q": "when do backups run", "expect": ["handbook.md"]})
        + "\n"
        + json.dumps({"q": "zzz nonexistent zzz", "expect": ["nowhere.md"]})
        + "\n",
        encoding="utf-8",
    )
    result = run_eval(db_path, fixtures, k=5)
    assert result.total == 2
    assert result.hit_at_k == 1
    assert result.misses == ["zzz nonexistent zzz"]
