"""Removing a collection — the counterpart `index_folder` never had."""

from __future__ import annotations

from pathlib import Path

import pytest

from uplink import db
from uplink.indexer import forget, index_folder
from uplink.search import search


@pytest.fixture
def two_collections(tmp_path: Path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "alpha.md").write_text("# Alpha\n\nAlpha covers backups nightly.", encoding="utf-8")
    (b / "bravo.md").write_text("# Bravo\n\nBravo covers escalation paging.", encoding="utf-8")
    dbp = tmp_path / "u.db"
    index_folder(a, dbp, collection="alpha")
    index_folder(b, dbp, collection="bravo")
    return dbp, a, b


def test_forget_one_collection_leaves_the_other(two_collections):
    dbp, _, _ = two_collections
    out = forget(dbp, "alpha")
    assert out["documents"] == 1 and out["chunks"] >= 1
    assert out["collections"] == ["alpha"]

    assert search(dbp, "backups nightly") == [], "removed content must not be retrievable"
    assert search(dbp, "escalation paging"), "the other collection must survive"


def test_forget_frees_the_collection_name_for_reuse(two_collections):
    """The corpus root must go too, or re-indexing a different folder into
    the same name is refused by a ghost of the old one."""
    dbp, _, b = two_collections
    forget(dbp, "alpha")
    index_folder(b, dbp, collection="alpha")  # different folder, same name
    assert search(dbp, "escalation paging", collection="alpha")


def test_forget_all_empties_the_index(two_collections):
    dbp, _, _ = two_collections
    out = forget(dbp, None)
    assert sorted(out["collections"]) == ["alpha", "bravo"]
    conn = db.connect_ro(dbp)
    try:
        assert conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) n FROM meta WHERE key LIKE 'corpus_root:%'"
        ).fetchone()["n"] == 0
    finally:
        conn.close()
    assert search(dbp, "backups") == [] and search(dbp, "escalation") == []


def test_forget_leaves_source_files_alone(two_collections):
    """Clearing the index must never delete the operator's documents."""
    dbp, a, b = two_collections
    forget(dbp, None)
    assert (a / "alpha.md").is_file()
    assert (b / "bravo.md").is_file()


def test_forget_unknown_collection_is_a_no_op(two_collections):
    dbp, _, _ = two_collections
    out = forget(dbp, "nosuch")
    assert out["documents"] == 0 and out["collections"] == []
    assert search(dbp, "backups nightly"), "nothing else may be disturbed"


def test_forget_rejects_a_bad_collection_name(two_collections):
    dbp, _, _ = two_collections
    with pytest.raises(ValueError):
        forget(dbp, "../evil")


def test_cli_refuses_without_yes(two_collections, capsys):
    """Deleting an index is quick to do and slow to undo."""
    from uplink.__main__ import main

    dbp, _, _ = two_collections
    assert main(["forget", "--db", str(dbp), "--all"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert search(dbp, "backups nightly"), "nothing removed without confirmation"


def test_cli_requires_a_target(two_collections, capsys):
    from uplink.__main__ import main

    dbp, _, _ = two_collections
    assert main(["forget", "--db", str(dbp), "--yes"]) == 2
    assert "collection" in capsys.readouterr().err


def test_cli_forget_all_reports_what_it_removed(two_collections, capsys):
    from uplink.__main__ import main

    dbp, _, _ = two_collections
    assert main(["forget", "--db", str(dbp), "--all", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "removed 2 documents" in out
    assert "not touched" in out
