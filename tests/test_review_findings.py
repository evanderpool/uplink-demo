"""Regression tests for the adversarial-review findings.

Each test pins a bug found in review: hostile characters in database paths
breaking the read-only URI, non-ASCII queries returning nothing, unicode
output crashing a piped Windows console, cross-corpus purging, section-title
leniency in the eval matcher, and markdown headings inside code fences.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from uplink import db
from uplink.evaluate import run_eval
from uplink.extractors import extract
from uplink.indexer import CorpusMismatch, index_folder
from uplink.search import search


def _make_corpus(tmp_path: Path, name: str, filename: str, text: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / filename).write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize("dirname", ["notes#v2", "temp%41dir", "with space"])
def test_connect_ro_survives_hostile_db_paths(tmp_path: Path, dirname: str):
    """'#' and '%' in a db path must neither break opening nor read-only."""
    corpus = _make_corpus(tmp_path, "c", "a.md", "# T\n\nthe password rotation runs monthly")
    db_path = tmp_path / dirname / "index.db"
    index_folder(corpus, db_path)

    hits = search(db_path, "password rotation", k=3)
    assert hits and hits[0].path == "a.md"

    conn = db.connect_ro(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    conn.close()
    # And no stray files were created by a mangled URI.
    assert not (tmp_path / "notes").exists()


def test_non_ascii_query_finds_non_ascii_content(tmp_path: Path):
    corpus = _make_corpus(tmp_path, "c", "menu.md", "# Menu\n\nThe café serves espresso.")
    db_path = tmp_path / "index.db"
    index_folder(corpus, db_path)
    hits = search(db_path, "café", k=3)
    assert hits and hits[0].path == "menu.md"


def test_cli_search_survives_unicode_on_piped_stdout(tmp_path: Path):
    """Piped stdout (cp1252 on Windows) must not crash on unicode corpus text."""
    corpus = _make_corpus(
        tmp_path, "c", "arrows.md", "# Flow\n\npipeline goes ingest → index → answer"
    )
    db_path = tmp_path / "index.db"
    index_folder(corpus, db_path)
    proc = subprocess.run(
        [sys.executable, "-m", "uplink", "search", "pipeline ingest", "--db", str(db_path)],
        capture_output=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"arrows.md" in proc.stdout


def test_second_corpus_into_same_db_is_refused(tmp_path: Path):
    corpus_a = _make_corpus(tmp_path, "a", "a.md", "alpha content here")
    corpus_b = _make_corpus(tmp_path, "b", "b.md", "beta content here")
    db_path = tmp_path / "index.db"
    index_folder(corpus_a, db_path)
    with pytest.raises(CorpusMismatch):
        index_folder(corpus_b, db_path)
    # Original corpus is intact.
    assert search(db_path, "alpha content", k=3)


def test_eval_matcher_ignores_section_title_decoys(tmp_path: Path):
    """A decoy doc whose SECTION title matches the expectation must not count."""
    root = tmp_path / "c"
    root.mkdir()
    (root / "decoy.md").write_text(
        "# Decoy\n\n## target-topic\n\nsphinx of black quartz judge my vow",
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    index_folder(root, db_path)
    fixtures = tmp_path / "g.jsonl"
    fixtures.write_text(
        json.dumps({"q": "sphinx of black quartz", "expect": ["target-topic.md"]}) + "\n",
        encoding="utf-8",
    )
    result = run_eval(db_path, fixtures, k=5)
    assert result.hit_at_k == 0  # decoy's section title must not satisfy a path expectation


def test_markdown_headings_inside_code_fences_are_not_sections(tmp_path: Path):
    p = tmp_path / "doc.md"
    p.write_text(
        "# Real Title\n\nintro text\n\n```bash\n# not a heading\necho hi\n```\n\nmore text\n",
        encoding="utf-8",
    )
    result = extract(p)
    titles = [s.title for s in result.sections]
    assert "not a heading" not in titles
    assert any("echo hi" in s.text for s in result.sections)


def test_touched_but_unchanged_file_skips_rechunk(tmp_path: Path):
    import os
    corpus = _make_corpus(tmp_path, "c", "a.md", "# T\n\nstable content")
    db_path = tmp_path / "index.db"
    index_folder(corpus, db_path)
    os.utime(corpus / "a.md")  # touch: new mtime, same bytes
    stats = index_folder(corpus, db_path)
    assert stats.indexed == 0
    assert stats.unchanged == 1
