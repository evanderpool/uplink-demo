"""Metrics surface, readable labels, and serving the original source file."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from test_webapp import _get, _make_server
from uplink import db, metrics
from uplink.originals import OriginalUnavailable, resolve_original
from uplink.webapp import readable_label


@pytest.fixture
def server(tmp_path: Path):
    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    yield url, db_path, tmp_path
    httpd.shutdown()
    httpd.server_close()


def _json(url: str, path: str) -> dict:
    _, body = _get(url + path)
    return json.loads(body)


# ------------------------------------------------------------------ labels

@pytest.mark.parametrize(
    "title,path,expected",
    [
        # A real extracted title wins untouched.
        ("Guideline for Disinfection and Sterilization", "cdc-disinfection.pdf",
         "Guideline for Disinfection and Sterilization"),
        # Filename fallbacks get tidied into something readable.
        ("nist-sp-800-53r5-security-controls", "nist-sp-800-53r5-security-controls.pdf",
         "NIST SP 800 53 R5 Security Controls"),
        ("apple-10k-fy2023", "apple-10k-fy2023.txt", "Apple 10-K FY 2023"),
        ("quarterly_budget_review", "quarterly_budget_review.xlsx",
         "Quarterly Budget Review"),
        (None, "runbook.md", "Runbook"),
        ("", "some.file.name.txt", "Some File Name"),
    ],
)
def test_readable_label(title, path, expected):
    assert readable_label(title, path) == expected


def test_labels_and_metadata_reach_the_sources_api(server):
    url, _, _ = server
    src = _json(url, "/api/sources")["sources"][0]
    assert src["label"] == "Runbook"
    assert src["filename"] == "runbook.md"
    assert src["size"] > 0
    assert src["viewable"] is True
    assert src["has_original"] is True


# ----------------------------------------------------------------- metrics

def test_metrics_reports_missing_accuracy_honestly(server):
    """No logged eval runs must read as 'not measured', never as a number."""
    url, _, _ = server
    m = _json(url, "/api/metrics")
    assert m["accuracy"]["available"] is False
    assert m["health"]["documents"] == 1
    assert m["health"]["chunks"] >= 1


def test_metrics_accuracy_from_history(tmp_path: Path):
    hist = tmp_path / "eval-history.jsonl"
    rows = [
        {"ts": "2026-08-01T00:00:00+00:00", "label": "baseline", "db": "uplink.db",
         "fixtures": "golden.jsonl", "questions": 18, "hit_at_1": 8,
         "hit_at_k": 12, "k": 5, "mrr": 0.532},
        {"ts": "2026-08-06T00:00:00+00:00", "label": "stopwords", "db": "uplink.db",
         "fixtures": "golden.jsonl", "questions": 18, "hit_at_1": 12,
         "hit_at_k": 16, "k": 5, "mrr": 0.769},
    ]
    hist.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    acc = metrics.accuracy(hist, db_name="uplink.db")
    assert acc["available"] is True
    assert acc["hit_at_1"] == round(12 / 18, 3)
    assert acc["hit_at_k"] == round(16 / 18, 3)
    assert acc["mrr"] == 0.769
    assert acc["label"] == "stopwords"
    assert [p["mrr"] for p in acc["series"]] == [0.532, 0.769]


def test_metrics_accuracy_survives_torn_history(tmp_path: Path):
    hist = tmp_path / "h.jsonl"
    hist.write_text(
        '{"ts":"t","questions":2,"hit_at_1":1,"hit_at_k":2,"k":5,"mrr":0.75}\n{"tor',
        encoding="utf-8",
    )
    assert metrics.accuracy(hist)["mrr"] == 0.75


def test_metrics_performance_and_feedback(tmp_path: Path):
    qlog = tmp_path / "query-log.jsonl"
    qlog.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": "t", "q": "a", "hits": 3, "latency_ms": 4.0},
        {"ts": "t", "q": "b", "hits": 0, "latency_ms": 10.0},
        {"ts": "t", "q": "c", "hits": 2, "latency_ms": 6.0},
        {"ts": "t", "kind": "doc", "path": "x.md"},
    ]), encoding="utf-8")
    perf = metrics.performance(qlog)
    assert perf["searches"] == 3
    assert perf["source_opens"] == 1
    assert perf["median_ms"] == 6.0
    assert perf["zero_hit_rate"] == round(1 / 3, 3)

    flog = tmp_path / "feedback.jsonl"
    flog.write_text("".join(json.dumps(r) + "\n" for r in [
        {"q": "a", "path": "one.md", "vote": "up"},
        {"q": "b", "path": "one.md", "vote": "up"},
        {"q": "c", "path": "two.md", "vote": "down"},
    ]), encoding="utf-8")
    promoted = tmp_path / "promoted.jsonl"
    promoted.write_text('{"q": "a", "expect": ["one.md"]}\n', encoding="utf-8")

    fb = metrics.feedback_loop(flog, promoted)
    assert fb["up"] == 2 and fb["down"] == 1
    assert fb["helpful_rate"] == round(2 / 3, 3)
    assert fb["promoted_fixtures"] == 1
    assert fb["pending_promotion"] == 1
    assert fb["by_document"][0]["path"] == "one.md"


def test_health_flags_unsearchable_documents(tmp_path: Path):
    """A document with no chunks is indexed but unsearchable — surface it."""
    dbp = tmp_path / "u.db"
    conn = db.connect_rw(dbp)
    conn.execute(
        "INSERT INTO documents(collection, path, filetype, sha256, mtime, size, "
        "title, indexed_at) VALUES ('main','empty.md','md','x',0,5,'Empty','t')"
    )
    conn.commit()
    conn.close()
    ro = db.connect_ro(dbp)
    try:
        h = metrics.health(ro)
    finally:
        ro.close()
    assert h["documents"] == 1
    assert h["documents_without_chunks"] == 1


# --------------------------------------------------------------- originals

def test_original_file_is_served(server):
    url, _, tmp_path = server
    with urllib.request.urlopen(
        url + "/api/file?collection=ops&path=runbook.md", timeout=10
    ) as resp:
        assert resp.status == 200
        assert "text/plain" in resp.headers["Content-Type"]
        assert "inline" in resp.headers["Content-Disposition"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        body = resp.read()
    # Byte-for-byte: the original is served as stored, not re-encoded.
    assert body == (tmp_path / "corpus" / "runbook.md").read_bytes()


def test_original_requires_both_identifiers(server):
    url, _, _ = server
    for q in ("", "?path=runbook.md", "?collection=ops"):
        try:
            _get(url + "/api/file" + q)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400


@pytest.mark.parametrize(
    "path",
    ["../../../etc/passwd", "..%2F..%2Fsecret.txt", "/etc/passwd",
     "C:\\Windows\\win.ini", "nosuch.md", "runbook.md/../../x"],
)
def test_original_cannot_escape_the_corpus(server, path: str):
    """The path is never taken from the client: it is looked up in the index
    and must resolve inside the collection's own recorded root."""
    url, _, _ = server
    try:
        _get(url + "/api/file?collection=ops&path=" + path)
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_original_refuses_when_file_moved(server, tmp_path: Path):
    url, _, tp = server
    (tp / "corpus" / "runbook.md").unlink()
    try:
        _get(url + "/api/file?collection=ops&path=runbook.md")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert b"no longer on disk" in exc.read()


def test_original_refuses_collection_without_root(tmp_path: Path):
    """Upload-only collections have no source folder; say so plainly."""
    dbp = tmp_path / "u.db"
    conn = db.connect_rw(dbp)
    conn.execute(
        "INSERT INTO documents(collection, path, filetype, sha256, mtime, size, "
        "title, indexed_at) VALUES ('ghost','a.md','md','x',0,5,'A','t')"
    )
    conn.commit()
    conn.close()
    ro = db.connect_ro(dbp)
    try:
        with pytest.raises(OriginalUnavailable, match="source folder"):
            resolve_original(ro, "ghost", "a.md")
    finally:
        ro.close()


def test_original_read_is_logged(server):
    url, db_path, _ = server
    _get(url + "/api/file?collection=ops&path=runbook.md")
    lines = [
        json.loads(x)
        for x in (db_path.parent / "query-log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(ln.get("kind") == "original" and ln["path"] == "runbook.md" for ln in lines)


def test_doc_endpoint_advertises_original_availability(server):
    url, _, _ = server
    doc = _json(url, "/api/doc?path=runbook.md&collection=ops")
    assert doc["has_original"] is True
    assert doc["viewable"] is True
    assert doc["label"] == "Runbook"


# ------------------- professional labels (proper casing + disambiguation)

@pytest.mark.parametrize(
    "path,expected",
    [
        # Vendor asset codes carry no meaning for a reader.
        ("ma658_macbook_air_late2008_userguide.pdf", "MacBook Air Late 2008 User Guide"),
        # Abbreviations expand; conventional casing is preserved.
        ("ma1966_mac-mini-m1-2020-qsg.pdf", "Mac Mini M1 2020 Quick Start Guide"),
        ("ma1733_imac-late2015-essentials.pdf", "iMac Late 2015 Essentials"),
        # Run-together tokens are split on known terms.
        ("ma564_powerbookg4_gettingstarted.pdf", "PowerBook G4 Getting Started"),
        # Specifications read the way they are written.
        ("macbook-pro-14inch-m4pro-2024-info.pdf",
         "MacBook Pro 14-inch M4 Pro 2024 Information"),
    ],
)
def test_labels_are_professionally_cased(path, expected):
    assert readable_label(None, path) == expected


def test_a_real_title_is_never_rewritten():
    """The author's name for a document beats anything derived from a
    filename, so an embedded title is used exactly as written."""
    assert readable_label(
        "PowerBook G4 12-inch (1.5 GHz) User's Guide (Manual)",
        "ma572_powerbookg4_12inch1_5ghzuserguide.pdf",
    ) == "PowerBook G4 12-inch (1.5 GHz) User's Guide (Manual)"


def test_duplicate_labels_are_disambiguated():
    """Vendors reuse one title across many files; a dozen sources all named
    'MacBook Pro' is a list you cannot navigate."""
    from uplink.webapp import disambiguate_labels

    rows = [
        {"label": "MacBook Pro", "path": "ma1526_macbook_pro_17inch_mid2010.pdf"},
        {"label": "MacBook Pro", "path": "ma1567_macbook_pro_15inch_early2011.pdf"},
        {"label": "iMac Quick Start", "path": "ma1728_imac-late2015-quickstart.pdf"},
    ]
    disambiguate_labels(rows)
    assert rows[0]["label"] == "MacBook Pro — 17-inch Mid 2010"
    assert rows[1]["label"] == "MacBook Pro — 15-inch Early 2011"
    # A label that was already unique is left alone.
    assert rows[2]["label"] == "iMac Quick Start"


def test_disambiguation_leaves_unique_labels_untouched():
    from uplink.webapp import disambiguate_labels

    rows = [{"label": "Alpha", "path": "a.md"}, {"label": "Bravo", "path": "b.md"}]
    disambiguate_labels(rows)
    assert [r["label"] for r in rows] == ["Alpha", "Bravo"]


def test_upload_reports_the_documents_real_name(tmp_path: Path):
    """A batch should show what it indexed, not a list of vendor asset
    codes: the extractor has just read the document's title, so the upload
    response carries the name it will be known by."""
    import json as _json

    from test_webapp import _make_server, _multipart, _post

    httpd, url, _ = _make_server(tmp_path, writes_enabled=True)
    try:
        body, ctype = _multipart({
            "collection": ("", b"notes"),
            "file": ("ma658_macbook_air_late2008_userguide.md",
                     b"# MacBook Air User Guide\n\nSetting up your MacBook Air."),
        })
        code, resp = _post(url + "/api/upload", body, ctype)
        assert code == 200, resp
        data = _json.loads(resp)
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert data["saved"] == "ma658_macbook_air_late2008_userguide.md"
    # The H1 wins over the filename, exactly as the sources list would show.
    assert data["label"] == "MacBook Air User Guide"


def test_upload_label_falls_back_to_a_cleaned_filename(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _multipart, _post

    httpd, url, _ = _make_server(tmp_path, writes_enabled=True)
    try:
        body, ctype = _multipart({
            "collection": ("", b"notes"),
            "file": ("ma1966_mac-mini-m1-2020-qsg.txt", b"Setting up your Mac mini."),
        })
        code, resp = _post(url + "/api/upload", body, ctype)
        data = _json.loads(resp)
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert code == 200
    assert data["label"] == "Mac Mini M1 2020 Quick Start Guide"


def test_every_indexable_type_can_be_served_as_an_original():
    """A document the indexer accepts but the reader refuses to display is a
    dead end the interface offers and cannot honour — so the two lists must
    not drift apart."""
    from uplink.extractors import SUPPORTED_EXTENSIONS
    from uplink.originals import CONTENT_TYPES

    missing = sorted(set(SUPPORTED_EXTENSIONS) - set(CONTENT_TYPES))
    assert not missing, f"indexable but not serveable: {missing}"
