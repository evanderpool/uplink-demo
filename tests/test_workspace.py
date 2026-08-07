"""The workspace endpoints: sources, per-source scoping, suggestions, notes."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from test_webapp import _get, _make_server, _post
from uplink.notes import add_note, delete_note, list_notes


@pytest.fixture
def server(tmp_path: Path):
    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    yield url, db_path
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def multi(tmp_path: Path):
    """Two collections with several documents — enough to scope against."""
    from http.server import ThreadingHTTPServer
    import threading

    from uplink.indexer import index_folder
    from uplink.webapp import make_handler

    ops = tmp_path / "ops"; ops.mkdir()
    (ops / "backup.md").write_text(
        "# Backups\n\n## Nightly\n\nBackups run nightly at 0200 and are kept 30 days.",
        encoding="utf-8")
    (ops / "escalation.md").write_text(
        "# Escalation\n\n## Paging\n\nSeverity-one incidents page the on-call engineer.",
        encoding="utf-8")
    fin = tmp_path / "fin"; fin.mkdir()
    (fin / "budget.md").write_text(
        "# Budget\n\n## Renewal\n\nThe budget renews in April each year.", encoding="utf-8")

    db_path = tmp_path / "data" / "uplink.db"
    index_folder(ops, db_path, collection="ops")
    index_folder(fin, db_path, collection="finance")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, None, True))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", db_path
    httpd.shutdown()
    httpd.server_close()


def _json(url: str, path: str) -> dict:
    _, body = _get(url + path)
    return json.loads(body)


# ----------------------------------------------------------------- sources

def test_sources_lists_documents_and_counts(multi):
    url, _ = multi
    data = _json(url, "/api/sources")
    paths = {s["path"] for s in data["sources"]}
    assert paths == {"backup.md", "escalation.md", "budget.md"}
    assert all(s["chunks"] >= 1 for s in data["sources"])
    assert {c["name"] for c in data["collections"]} == {"ops", "finance"}


def test_sources_scoped_to_one_collection(multi):
    url, _ = multi
    data = _json(url, "/api/sources?collection=ops")
    assert {s["path"] for s in data["sources"]} == {"backup.md", "escalation.md"}
    assert data["collection"] == "ops"


def test_sources_rejects_bad_collection(multi):
    url, _ = multi
    try:
        _get(url + "/api/sources?collection=..%2Fevil")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_suggestions_are_grounded_and_stable(multi):
    """Opening questions come from the index, so they can never propose a
    topic the corpus does not contain — and they don't drift between loads."""
    url, _ = multi
    first = _json(url, "/api/sources?collection=ops")["suggestions"]
    second = _json(url, "/api/sources?collection=ops")["suggestions"]
    assert first == second
    assert first, "a non-empty collection should offer opening questions"
    blob = " ".join(first).lower()
    assert "nightly" in blob or "escalation" in blob or "paging" in blob or "backup" in blob


def test_suggestions_empty_for_empty_collection(tmp_path: Path):
    from uplink import db
    from uplink.suggest import suggestions

    dbp = tmp_path / "empty.db"
    db.connect_rw(dbp).close()
    conn = db.connect_ro(dbp)
    try:
        assert suggestions(conn) == []
    finally:
        conn.close()


def test_suggestions_skip_page_number_sections(tmp_path: Path):
    """PDF sections are 'Page 31' — useless as a topic, so they must not
    become questions."""
    import sqlite3

    from uplink import db
    from uplink.suggest import suggestions

    dbp = tmp_path / "pdfish.db"
    conn = db.connect_rw(dbp)
    conn.execute(
        "INSERT INTO documents(collection, path, filetype, sha256, mtime, size, title, indexed_at)"
        " VALUES ('main','manual.pdf','pdf','x',0,10,'Manual','2026-01-01T00:00:00')"
    )
    for seq, section in enumerate(["Page 1", "Page 2", "Page 3"]):
        conn.execute(
            "INSERT INTO chunks(doc_id, seq, section, text) VALUES (1, ?, ?, 'body text here')",
            (seq, section),
        )
    conn.commit()
    conn.close()
    ro: sqlite3.Connection = db.connect_ro(dbp)
    try:
        out = suggestions(ro)
    finally:
        ro.close()
    assert out, "should still offer document-level prompts"
    assert not any("Page 1" in q or "Page 2" in q for q in out)


# --------------------------------------------------- per-source scoping

def test_search_scoped_to_selected_sources(multi):
    """Deselecting a source must remove it from retrieval, not just hide it."""
    url, _ = multi
    everything = _json(url, "/api/search?q=backups%20nightly%20budget%20April")
    assert {h["path"] for h in everything["hits"]} >= {"backup.md", "budget.md"}

    scoped = _json(
        url,
        "/api/search?q=backups%20nightly%20budget%20April&scoped=1&doc=finance%2Fbudget.md",
    )
    assert {h["path"] for h in scoped["hits"]} == {"budget.md"}


def test_search_with_multiple_selected_docs(multi):
    url, _ = multi
    q = ("/api/search?q=backups%20escalation%20budget&scoped=1"
         "&doc=ops%2Fbackup.md&doc=ops%2Fescalation.md")
    hits = _json(url, q)["hits"]
    assert hits
    assert {h["path"] for h in hits} <= {"backup.md", "escalation.md"}


def test_exclusion_scoping(multi):
    """Large corpora send the short side: exclusions instead of inclusions."""
    url, _ = multi
    data = _json(url, "/api/search?q=backups%20budget&scoped=1&xdoc=finance%2Fbudget.md")
    assert data["hits"]
    assert "budget.md" not in {h["path"] for h in data["hits"]}


def test_empty_selection_over_http_retrieves_nothing(multi):
    """THE regression that mattered: with every box unchecked the request
    carries `scoped=1` and no docs, and the server must return nothing —
    not silently fall back to searching the whole corpus."""
    url, _ = multi
    unscoped = _json(url, "/api/search?q=backups%20budget%20escalation")
    assert unscoped["hits"], "sanity: the query does match without scoping"

    scoped = _json(url, "/api/search?q=backups%20budget%20escalation&scoped=1")
    assert scoped["hits"] == []


def test_same_filename_in_two_collections_scopes_independently(tmp_path: Path):
    """A document is (collection, path). Keying on path alone let one
    checkbox control two different documents."""
    from http.server import ThreadingHTTPServer
    import threading

    from uplink.indexer import index_folder
    from uplink.webapp import make_handler

    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "shared.md").write_text("# Shared\n\nalpha zephyr content", encoding="utf-8")
    (b / "shared.md").write_text("# Shared\n\nbravo zephyr content", encoding="utf-8")
    dbp = tmp_path / "data" / "uplink.db"
    index_folder(a, dbp, collection="alpha")
    index_folder(b, dbp, collection="bravo")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dbp, None, True))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        both = _json(url, "/api/search?q=zephyr")["hits"]
        assert {h["collection"] for h in both} == {"alpha", "bravo"}

        only_a = _json(url, "/api/search?q=zephyr&scoped=1&doc=alpha%2Fshared.md")["hits"]
        assert only_a and {h["collection"] for h in only_a} == {"alpha"}

        not_b = _json(url, "/api/search?q=zephyr&scoped=1&xdoc=bravo%2Fshared.md")["hits"]
        assert not_b and {h["collection"] for h in not_b} == {"alpha"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_search_with_unknown_doc_returns_nothing(multi):
    url, _ = multi
    assert _json(url, "/api/search?q=backups&scoped=1&doc=ops%2Fnosuch.md")["hits"] == []


def test_scoping_survives_hostile_doc_values(multi):
    """Malformed values are dropped, never widened into an unscoped search."""
    url, _ = multi
    for bad in ("..%2F..%2Fetc%2Fpasswd", "nocollection.md", "%2Fleading", "UPPER%2Fx.md"):
        data = _json(url, "/api/search?q=backups&scoped=1&doc=" + bad)
        assert data["hits"] == [], bad


def test_oversized_selection_errors_instead_of_truncating(multi):
    """Silently dropping paths past the cap made documents unsearchable with
    no signal; too many now fails loudly."""
    url, _ = multi
    docs = "".join(f"&doc=ops%2Ff{i}.md" for i in range(250))
    try:
        _get(url + "/api/search?q=backups&scoped=1" + docs)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
        assert b"too many" in exc.read()


def test_empty_selection_retrieves_nothing_at_the_function_level(tmp_path: Path):
    from uplink.indexer import index_folder
    from uplink.search import search

    root = tmp_path / "c"; root.mkdir()
    (root / "a.md").write_text("# A\n\nalpha content here", encoding="utf-8")
    dbp = tmp_path / "u.db"
    index_folder(root, dbp, collection="main")
    assert search(dbp, "alpha", include=[]) == []
    assert search(dbp, "alpha", include=[("main", "a.md")])
    assert search(dbp, "alpha", exclude=[("main", "a.md")]) == []


# ------------------------------------------------------------------- notes

def test_note_roundtrip_via_api(server):
    url, db_path = server
    payload = {
        "title": "how do restarts work",
        "body": "Restart the bridge with run_bridge after config changes.",
        "citations": [{"path": "runbook.md", "section": "Restarts", "seq": 0,
                       "collection": "ops"}],
        "collection": "ops",
    }
    code, resp = _post(url + "/api/notes", json.dumps(payload).encode(), "application/json")
    assert code == 200, resp
    note_id = json.loads(resp)["note"]["id"]

    listed = _json(url, "/api/notes")["notes"]
    assert listed[0]["id"] == note_id
    assert listed[0]["citations"][0]["seq"] == 0

    code, _ = _post(url + "/api/notes/delete",
                    json.dumps({"id": note_id}).encode(), "application/json")
    assert code == 200
    assert _json(url, "/api/notes")["notes"] == []


def test_notes_require_a_body(server):
    url, _ = server
    code, resp = _post(url + "/api/notes", json.dumps({"title": "x"}).encode(),
                       "application/json")
    assert code == 400
    assert b"body" in resp


def test_notes_refused_when_not_loopback(tmp_path: Path):
    httpd, url, _ = _make_server(tmp_path, writes_enabled=False)
    try:
        code, _ = _post(url + "/api/notes",
                        json.dumps({"body": "x"}).encode(), "application/json")
        assert code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_delete_unknown_note_is_404(server):
    url, _ = server
    code, _ = _post(url + "/api/notes/delete",
                    json.dumps({"id": "a" * 12}).encode(), "application/json")
    assert code == 404


def test_note_id_validation(tmp_path: Path):
    with pytest.raises(ValueError):
        delete_note(tmp_path / "notes.jsonl", "../../etc")


def test_notes_survive_torn_line(tmp_path: Path):
    """A crash mid-write must cost exactly one record — not the one after it.
    Appending onto an unterminated line would fuse and lose both."""
    notes = tmp_path / "notes.jsonl"
    add_note(notes, "kept", "body one")
    with notes.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "torn')
    add_note(notes, "also kept", "body two")
    titles = [n["title"] for n in list_notes(notes)]
    assert set(titles) == {"kept", "also kept"}


def test_every_jsonl_log_heals_a_torn_tail(tmp_path: Path):
    """Same guarantee for the query, feedback, and eval-history logs."""
    from uplink.feedback import append_jsonl

    log = tmp_path / "query-log.jsonl"
    append_jsonl(log, {"q": "first"})
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"q": "tor')
    append_jsonl(log, {"q": "second"})
    good = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            good.append(json.loads(line)["q"])
        except (json.JSONDecodeError, KeyError):
            pass
    assert good == ["first", "second"]


def test_note_citations_are_cleaned(tmp_path: Path):
    """A saved note must stay clickable: junk citations are dropped, good
    ones keep their anchor."""
    notes = tmp_path / "notes.jsonl"
    note = add_note(
        notes, "t", "b",
        citations=[
            {"path": "a.md", "section": "S", "seq": 3, "collection": "ops"},
            {"section": "no path"},
            None,
            "not-a-dict",
            {"path": "b.md", "seq": "not-an-int"},
        ],
    )
    assert [c["path"] for c in note["citations"]] == ["a.md", "b.md"]
    assert note["citations"][0]["seq"] == 3
    assert "seq" not in note["citations"][1]


def test_notes_filtered_by_collection(tmp_path: Path):
    notes = tmp_path / "notes.jsonl"
    add_note(notes, "ops note", "b", collection="ops")
    add_note(notes, "fin note", "b", collection="finance")
    assert [n["title"] for n in list_notes(notes, "ops")] == ["ops note"]
    assert len(list_notes(notes)) == 2
