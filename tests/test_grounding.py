"""Answers may only come from the knowledge base, and only from the sources
the operator selected.

Both were previously promises rather than properties: the source checkboxes
never reached the ask path, and nothing checked what an answer cited. These
tests hold both closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uplink.asks import (
    GroundingError,
    check_grounding,
    new_ask,
    read_request,
    write_answer,
)


@pytest.fixture
def indexed(tmp_path: Path):
    """Two documents in one collection, plus a queue directory."""
    from uplink.indexer import index_folder

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "alpha.md").write_text("# Alpha\n\n## One\n\nAlpha discusses backups.",
                                   encoding="utf-8")
    (root / "beta.md").write_text("# Beta\n\n## Two\n\nBeta discusses escalation.",
                                  encoding="utf-8")
    dbp = tmp_path / "data" / "uplink.db"
    index_folder(root, dbp, collection="ops")
    return dbp, tmp_path / "data" / "asks"


# ------------------------------------------------- scope travels with the ask

def test_ask_carries_the_source_selection(indexed):
    _, asks = indexed
    req = new_ask(asks, "q", "ops", k=8, docs=["ops/alpha.md"])
    stored = read_request(asks, req["id"])
    assert stored["docs"] == ["ops/alpha.md"], (
        "the selection must be stored: the brain answers later and cannot "
        "see the checkboxes"
    )


def test_unscoped_ask_records_no_docs_key(indexed):
    _, asks = indexed
    req = new_ask(asks, "q", "ops", k=8)
    assert "docs" not in read_request(asks, req["id"])


def test_empty_selection_is_distinguishable_from_no_scoping(indexed):
    """[] means 'nothing selected'; absent means 'everything'. Collapsing
    them is how a scoped question quietly becomes unscoped."""
    _, asks = indexed
    empty = new_ask(asks, "q", "ops", docs=[])
    assert read_request(asks, empty["id"])["docs"] == []


# --------------------------------------------------- grounding enforcement

def test_citation_outside_the_selection_is_refused(indexed):
    """THE regression: an answer must not cite a document the operator
    deselected."""
    dbp, asks = indexed
    req = new_ask(asks, "q", "ops", docs=["ops/alpha.md"])
    with pytest.raises(GroundingError, match="outside the sources selected"):
        write_answer(asks, req["id"], "text",
                     citations=[{"path": "beta.md", "collection": "ops"}],
                     db_path=dbp)
    assert not (asks / f"{req['id']}.response.json").exists(), (
        "a refused answer must not be published"
    )


def test_citation_inside_the_selection_is_accepted(indexed):
    dbp, asks = indexed
    req = new_ask(asks, "q", "ops", docs=["ops/alpha.md"])
    write_answer(asks, req["id"], "text",
                 citations=[{"path": "alpha.md", "collection": "ops", "seq": 0}],
                 db_path=dbp)
    resp = json.loads((asks / f"{req['id']}.response.json").read_text(encoding="utf-8"))
    assert resp["grounding_verified"] is True
    assert resp["scoped"] is True
    assert resp["sources_used"] == [{"collection": "ops", "path": "alpha.md"}]
    assert resp["passages"] == 1


def test_citation_not_in_the_knowledge_base_is_refused(indexed):
    """An answer may only cite indexed documents — this is what stops
    outside knowledge being attributed to the corpus."""
    dbp, asks = indexed
    req = new_ask(asks, "q", "ops")
    for bogus in ("wikipedia.org", "gamma.md", "alpha.md.backup"):
        with pytest.raises(GroundingError, match="not in the knowledge base"):
            write_answer(asks, req["id"], "text",
                         citations=[{"path": bogus, "collection": "ops"}],
                         db_path=dbp)


def test_unscoped_ask_may_cite_any_indexed_document(indexed):
    dbp, asks = indexed
    req = new_ask(asks, "q", "ops")
    write_answer(asks, req["id"], "text",
                 citations=[{"path": "alpha.md", "collection": "ops"},
                            {"path": "beta.md", "collection": "ops"}],
                 db_path=dbp)
    resp = json.loads((asks / f"{req['id']}.response.json").read_text(encoding="utf-8"))
    assert len(resp["sources_used"]) == 2
    assert resp["scoped"] is False


def test_collection_is_inferred_and_recorded(indexed):
    """A citation without a collection is resolved against the index, so the
    provenance record always names a real (collection, path) pair."""
    dbp, asks = indexed
    req = new_ask(asks, "q", None)
    write_answer(asks, req["id"], "text",
                 citations=[{"path": "alpha.md"}], db_path=dbp)
    resp = json.loads((asks / f"{req['id']}.response.json").read_text(encoding="utf-8"))
    assert resp["citations"][0]["collection"] == "ops"


def test_answer_written_without_db_is_marked_unverified(indexed):
    """Skipping the check is allowed but never silent — the interface shows
    'unverified' so trust is never assumed."""
    _, asks = indexed
    req = new_ask(asks, "q", "ops", docs=["ops/alpha.md"])
    write_answer(asks, req["id"], "text",
                 citations=[{"path": "beta.md", "collection": "ops"}])
    resp = json.loads((asks / f"{req['id']}.response.json").read_text(encoding="utf-8"))
    assert resp["grounding_verified"] is False


def test_error_answers_skip_grounding(indexed):
    """Reporting a failure must always be possible, even with no citations."""
    dbp, asks = indexed
    req = new_ask(asks, "q", "ops", docs=[])
    write_answer(asks, req["id"], "", state="error",
                 error="no sources selected", db_path=dbp)
    resp = json.loads((asks / f"{req['id']}.response.json").read_text(encoding="utf-8"))
    assert resp["state"] == "error"


def test_check_grounding_drops_malformed_citations(indexed):
    dbp, asks = indexed
    out = check_grounding(dbp, {"collection": "ops"},
                          [None, "string", {}, {"path": ""},
                           {"path": "alpha.md"}])
    assert [c["path"] for c in out] == ["alpha.md"]


# ------------------------------------------------------- the HTTP surface

def test_ask_endpoint_stores_the_selection(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        payload = {"q": "how do restarts work", "collection": "ops",
                   "scoped": True, "docs": ["ops/runbook.md", "ops/nope.md"]}
        code, resp = _post(url + "/api/ask",
                           _json.dumps(payload).encode(), "application/json")
        assert code == 200, resp
        body = _json.loads(resp)
        assert body["scoped_to"] == 2
        ask_id = body["id"]
    finally:
        httpd.shutdown()
        httpd.server_close()

    stored = read_request(db_path.parent / "asks", ask_id)
    assert stored["docs"] == ["ops/runbook.md", "ops/nope.md"]


def test_ask_endpoint_records_an_empty_selection(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        code, resp = _post(
            url + "/api/ask",
            _json.dumps({"q": "q", "scoped": True, "docs": []}).encode(),
            "application/json",
        )
        ask_id = _json.loads(resp)["id"]
        assert _json.loads(resp)["scoped_to"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert read_request(db_path.parent / "asks", ask_id)["docs"] == []


def test_unscoped_request_leaves_scope_absent(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        code, resp = _post(url + "/api/ask", _json.dumps({"q": "q"}).encode(),
                           "application/json")
        ask_id = _json.loads(resp)["id"]
        assert _json.loads(resp)["scoped_to"] is None
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "docs" not in read_request(db_path.parent / "asks", ask_id)


# ----------------------------------------------- stale-page detection

def test_index_html_carries_a_build_stamp(tmp_path: Path):
    """A tab runs the JS it loaded; without a stamp it cannot know it is
    behind, which is exactly how a source selection silently failed to
    travel while the UI looked current."""
    import urllib.request

    from test_webapp import _make_server
    from uplink.webapp import build_id

    httpd, url, _ = _make_server(tmp_path, writes_enabled=True)
    try:
        with urllib.request.urlopen(url + "/", timeout=10) as resp:
            body = resp.read().decode("utf-8")
        with urllib.request.urlopen(url + "/api/status", timeout=10) as resp:
            status = json.loads(resp.read())
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert "__BUILD__" not in body, "the placeholder must be substituted"
    assert build_id() in body
    assert status["build"] == build_id(), "page and server must be comparable"


def test_build_id_changes_when_an_asset_changes(tmp_path: Path, monkeypatch):
    import time as _time

    from uplink import webapp

    original = (webapp.STATIC_DIR / "app.js").read_bytes()
    first = webapp.build_id()
    try:
        _time.sleep(0.01)
        (webapp.STATIC_DIR / "app.js").write_bytes(original + b"\n// touch\n")
        assert webapp.build_id() != first
    finally:
        (webapp.STATIC_DIR / "app.js").write_bytes(original)
    assert webapp.build_id() != "" and webapp.build_id() is not None
