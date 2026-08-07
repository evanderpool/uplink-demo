"""The source viewer: citations must be openable, and openable only from
the index — the endpoint reads chunks, never the filesystem."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from test_webapp import _get, _make_server


@pytest.fixture
def server(tmp_path: Path):
    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    yield url, db_path
    httpd.shutdown()
    httpd.server_close()


def _doc(url: str, query: str) -> dict:
    _, body = _get(url + "/api/doc?" + query)
    return json.loads(body)


def test_doc_returns_indexed_text(server):
    url, _ = server
    doc = _doc(url, "path=runbook.md&collection=ops")
    assert doc["path"] == "runbook.md"
    assert doc["collection"] == "ops"
    assert doc["total_chunks"] >= 1
    assert "run_bridge" in doc["chunks"][0]["text"]
    assert doc["chunks"][0]["section"] == "Restarts"


def test_doc_without_collection_still_resolves(server):
    url, _ = server
    assert _doc(url, "path=runbook.md")["collection"] == "ops"


def test_doc_requires_path(server):
    url, _ = server
    try:
        _get(url + "/api/doc")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


@pytest.mark.parametrize(
    "path",
    [
        "../uplink/db.py",
        "..%2F..%2Fsecret.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "runbook.md/../../../secrets",
        "nonexistent.md",
    ],
)
def test_doc_cannot_reach_anything_outside_the_index(server, path: str):
    """The lookup is an exact parameterized match in `documents`, so there is
    no file read to traverse: anything not indexed is simply 404."""
    url, _ = server
    try:
        _, body = _get(url + "/api/doc?path=" + path.replace(" ", "%20"))
        code = 200
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read()
    assert code == 404, (path, body[:200])


def test_doc_rejects_bad_collection(server):
    url, _ = server
    try:
        _get(url + "/api/doc?path=runbook.md&collection=..%2Fevil")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_doc_window_centres_on_the_cited_chunk(tmp_path: Path):
    """A citation deep in a long document opens near that passage, not at
    the top of the file."""
    from uplink.indexer import index_folder
    from uplink.webapp import make_handler
    from http.server import ThreadingHTTPServer
    import threading

    root = tmp_path / "corpus"
    root.mkdir()
    body = "\n\n".join(
        f"## Section {i}\n\nParagraph {i} " + ("filler words " * 120)
        for i in range(30)
    )
    (root / "long.md").write_text("# Long\n\n" + body, encoding="utf-8")
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="main")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, None, True))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        full = _doc(url, "path=long.md")
        assert full["total_chunks"] > 12, full["total_chunks"]

        deep = _doc(url, "path=long.md&seq=20&limit=9")
        seqs = [c["seq"] for c in deep["chunks"]]
        assert 20 in seqs
        assert deep["start"] > 0
        # Paging forward from the window start stays inside the document.
        nxt = _doc(url, f"path=long.md&seq={deep['start'] + len(seqs)}&limit=9")
        assert nxt["chunks"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_doc_limit_is_capped(server):
    url, _ = server
    doc = _doc(url, "path=runbook.md&limit=9999")
    assert len(doc["chunks"]) <= 40


def test_doc_survives_junk_params(server):
    url, _ = server
    doc = _doc(url, "path=runbook.md&seq=abc&limit=xyz")
    assert doc["chunks"]


# ------------------ pins from the v0.4 doc-surface adversarial review

def test_paging_covers_every_chunk_with_no_gaps(tmp_path: Path):
    """Backward paging must not skip chunks. Centring on a seq shifts the
    window by limit/2 as well, so paging uses an absolute `start`."""
    from uplink.indexer import index_folder
    from uplink.webapp import make_handler
    from http.server import ThreadingHTTPServer
    import threading

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "long.md").write_text(
        "# Long\n\n" + "\n\n".join(
            f"## S{i}\n\nParagraph {i} " + ("filler " * 150) for i in range(40)
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="main")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, None, True))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        opened = _doc(url, "path=long.md&seq=30&limit=9")
        total = opened["total_chunks"]
        assert total > 20

        # Walk backward the way the UI does, collecting what a reader sees.
        seen, start, shown = set(), opened["start"], len(opened["chunks"])
        seen.update(c["seq"] for c in opened["chunks"])
        while start > 0:
            start = max(0, start - shown)
            page = _doc(url, f"path=long.md&start={start}&limit=9")
            shown = len(page["chunks"])
            assert page["start"] == start
            seen.update(c["seq"] for c in page["chunks"])
        assert seen >= set(range(0, opened["start"] + len(opened["chunks"]))), \
            sorted(set(range(opened["start"])) - seen)

        # And forward to the end.
        start = opened["start"]
        while start + shown < total:
            start += shown
            page = _doc(url, f"path=long.md&start={start}&limit=9")
            shown = len(page["chunks"])
            seen.update(c["seq"] for c in page["chunks"])
        assert seen == set(range(total))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ambiguous_path_refuses_rather_than_guessing(tmp_path: Path):
    """The same filename in two collections must never be resolved by
    guessing — that would show unrelated text as the source of a claim."""
    from uplink.indexer import index_folder
    from uplink.webapp import make_handler
    from http.server import ThreadingHTTPServer
    import threading

    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    (a / "notes.md").write_text("# Notes\n\nAlpha public content.", encoding="utf-8")
    (b / "notes.md").write_text("# Notes\n\nBravo confidential content.", encoding="utf-8")
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(a, db_path, collection="alpha")
    index_folder(b, db_path, collection="bravo")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, None, True))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        try:
            _get(url + "/api/doc?path=notes.md")
            raise AssertionError("expected 409")
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
            payload = json.loads(exc.read())
            assert sorted(payload["collections"]) == ["alpha", "bravo"]
            assert "confidential" not in json.dumps(payload).lower()
        # Naming the collection resolves it.
        doc = _doc(url, "path=notes.md&collection=bravo")
        assert "Bravo confidential" in doc["chunks"][0]["text"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_oversized_seq_does_not_500(server):
    """int64 overflow reached the SQLite bind and raised OverflowError."""
    url, _ = server
    doc = _doc(url, "path=runbook.md&seq=" + "9" * 40)
    assert doc["chunks"]
    doc = _doc(url, "path=runbook.md&start=" + "9" * 40)
    assert doc["start"] == 0


def test_doc_reads_are_logged(server):
    """/api/doc returns untruncated text, so it belongs in the audit trail."""
    url, db_path = server
    _doc(url, "path=runbook.md&collection=ops")
    lines = [
        json.loads(x)
        for x in (db_path.parent / "query-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    docs = [ln for ln in lines if ln.get("kind") == "doc"]
    assert docs and docs[-1]["path"] == "runbook.md"
    assert docs[-1]["collection"] == "ops"


def test_doc_endpoint_is_read_only_when_writes_disabled(tmp_path: Path):
    """Verification must work on a phone too: reading a source is a GET, so
    it survives the non-loopback bind that disables every write."""
    httpd, url, _ = _make_server(tmp_path, writes_enabled=False)
    try:
        assert _doc(url, "path=runbook.md")["chunks"]
    finally:
        httpd.shutdown()
        httpd.server_close()
