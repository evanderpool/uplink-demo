"""Web UI: serves the page, answers search/status, and enforces the
localhost-only write rule — writes exist only on a loopback bind."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from uplink.indexer import index_folder
from uplink.webapp import make_handler


def _make_server(tmp_path: Path, writes_enabled: bool):
    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    (root / "runbook.md").write_text(
        "# Runbook\n\n## Restarts\n\nRestart the bridge with run_bridge after config changes.",
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="ops")

    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "health.html").write_text("<!doctype html><title>h</title>ok", encoding="utf-8")

    # An isolated fixtures dir: a test server must never report the repo's
    # eval history as if it were this corpus's accuracy.
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(db_path, reports, writes_enabled,
                     fixtures_dir=tmp_path / "fixtures"),
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", db_path


@pytest.fixture
def server(tmp_path: Path):
    """A server as it would run bound to a non-loopback address: no writes."""
    httpd, url, _ = _make_server(tmp_path, writes_enabled=False)
    yield url
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def rw_server(tmp_path: Path):
    """A server as it runs on the operator's desktop: loopback, writes on."""
    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    yield url, db_path
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read()


def _post(url: str, data: bytes, content_type: str, extra: dict | None = None,
          xuplink: bool = True):
    headers = {"Content-Type": content_type}
    if xuplink:
        headers["X-Uplink"] = "1"
    headers.update(extra or {})
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _multipart(fields: dict[str, tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = "testboundary123"
    out = b""
    for name, (filename, payload) in fields.items():
        out += f"--{boundary}\r\n".encode()
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        out += disp.encode() + b"\r\n\r\n" + payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


# ------------------------------------------------------------- read surface

def test_index_page_serves(server):
    status, body = _get(server + "/")
    assert status == 200
    assert b"Uplink" in body
    assert b"panel-sources" in body and b"panel-studio" in body  # the workspace shell


def test_static_assets_serve_with_correct_types(server):
    for name, ctype in (
        ("app.css", "text/css"),
        ("app.js", "text/javascript"),
        ("gsap.min.js", "text/javascript"),
    ):
        with urllib.request.urlopen(server + "/static/" + name, timeout=10) as resp:
            assert resp.status == 200
            assert ctype in resp.headers["Content-Type"]
            assert int(resp.headers["Content-Length"]) > 0


@pytest.mark.parametrize(
    "name",
    ["../webapp.py", "..%2Fwebapp.py", "secrets.env", "index.html/../db.py", "NOTICE.md"],
)
def test_static_is_a_whitelist_not_a_directory(server, name: str):
    """The static dir is application code, not a file-read surface."""
    try:
        code, _ = _get(server + "/static/" + name)
    except urllib.error.HTTPError as exc:
        code = exc.code
    assert code == 404, name


def test_api_search_returns_cited_hits(server):
    status, body = _get(server + "/api/search?q=restart%20bridge&k=5")
    assert status == 200
    data = json.loads(body)
    assert data["hits"], data
    hit = data["hits"][0]
    assert hit["path"] == "runbook.md"
    assert hit["section"] == "Restarts"
    assert hit["collection"] == "ops"
    assert "score" in hit and "snippet" in hit
    assert isinstance(data["latency_ms"], (int, float))


def test_api_search_collection_filter(server):
    status, body = _get(server + "/api/search?q=restart&collection=ops")
    assert json.loads(body)["hits"]
    status, body = _get(server + "/api/search?q=restart&collection=nosuch")
    assert json.loads(body)["hits"] == []


def test_api_search_rejects_bad_collection(server):
    try:
        _get(server + "/api/search?q=x&collection=..%2Fevil")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_api_search_requires_query(server):
    try:
        _get(server + "/api/search")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_api_search_survives_hostile_query(server):
    status, body = _get(server + '/api/search?q=%22unclosed%20NEAR(a%20b)%20*')
    assert status == 200
    json.loads(body)


def test_api_status(server):
    status, body = _get(server + "/api/status")
    data = json.loads(body)
    assert status == 200
    assert data["documents"] == 1
    assert data["reports"] == ["health.html"]
    assert data["writes"] is False
    assert data["collections"] == [{"name": "ops", "documents": 1, "chunks": data["chunks"]}]


def test_reports_whitelist_blocks_traversal(server):
    for path in ("/reports/../uplink/db.py", "/reports/secret.html", "/reports/%2e%2e/x"):
        try:
            code, _ = _get(server + path)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404, path
    code, body = _get(server + "/reports/health.html")
    assert code == 200 and b"ok" in body


def test_unknown_path_404(server):
    try:
        _get(server + "/admin")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


# ------------------------------------------- the localhost-only write rule

def test_writes_refused_when_not_loopback(server):
    body, ctype = _multipart({"file": ("a.md", b"# hi"), "collection": ("", b"ops")})
    code, resp = _post(server + "/api/upload", body, ctype)
    assert code == 403
    code, resp = _post(
        server + "/api/feedback",
        json.dumps({"q": "x", "path": "runbook.md", "vote": "up"}).encode(),
        "application/json",
    )
    assert code == 403


def test_put_delete_rejected_even_on_rw(rw_server):
    url, _ = rw_server
    req = urllib.request.Request(url + "/api/upload", data=b"x", method="PUT")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 405")
    except urllib.error.HTTPError as exc:
        assert exc.code == 405


# ------------------------------------------- browser-borne attack defenses
# (pinned from the v0.2 adversarial logic review)

def test_post_without_xuplink_header_403(rw_server):
    """CSRF: a cross-origin form/fetch cannot attach custom headers without a
    preflight this server never grants — so no header, no write."""
    url, _ = rw_server
    body, ctype = _multipart({"file": ("a.md", b"# a"), "collection": ("", b"notes")})
    code, resp = _post(url + "/api/upload", body, ctype, xuplink=False)
    assert code == 403
    assert b"X-Uplink" in resp


def test_post_with_hostile_origin_403(rw_server):
    url, _ = rw_server
    payload = json.dumps({"q": "x", "path": "runbook.md", "vote": "up"}).encode()
    code, resp = _post(
        url + "/api/feedback", payload, "application/json",
        extra={"Origin": "https://evil.example"},
    )
    assert code == 403
    assert b"cross-origin" in resp


def test_dns_rebinding_host_rejected(server):
    """A request whose Host header names a foreign domain is refused — the
    rebinding trick that makes corpus reads same-origin for an attacker."""
    req = urllib.request.Request(server + "/api/search?q=restart")
    req.add_unredirected_header("Host", "evil.example")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 403")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403


def test_negative_content_length_rejected(rw_server):
    """Content-Length: -1 must not reach rfile.read(-1) (read-to-EOF would
    bypass every size cap)."""
    import socket

    url, _ = rw_server
    port = int(url.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(
            b"POST /api/feedback HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nX-Uplink: 1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: -1\r\n\r\n"
        )
        first_line = s.recv(65536).split(b"\r\n", 1)[0]
    assert b"400" in first_line


# ----------------------------------------------------------------- uploads

def test_upload_indexes_document(rw_server):
    url, db_path = rw_server
    body, ctype = _multipart(
        {
            "collection": ("", b"notes"),
            "file": ("memo.md", "# Memo\n\nThe kickoff call is on Thursday.".encode()),
        }
    )
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 200, resp
    data = json.loads(resp)
    assert data["saved"] == "memo.md"
    assert data["indexed"] == 1 and data["errors"] == []
    saved = db_path.parent / "uploads" / "notes" / "memo.md"
    assert saved.is_file()

    _, sbody = _get(url + "/api/search?q=kickoff%20call&collection=notes")
    hits = json.loads(sbody)["hits"]
    assert hits and hits[0]["path"] == "memo.md"


def test_upload_sanitizes_traversal_filename(rw_server):
    url, db_path = rw_server
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("../../../evil.md", b"# evil")}
    )
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 200, resp
    assert json.loads(resp)["saved"] == "evil.md"
    # The file landed inside the uploads dir, nowhere else.
    assert (db_path.parent / "uploads" / "notes" / "evil.md").is_file()
    assert not (db_path.parent.parent / "evil.md").exists()


def test_upload_rejects_unsupported_extension(rw_server):
    url, _ = rw_server
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("shell.exe", b"MZ....")}
    )
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 400
    assert b"unsupported" in resp


def test_upload_refuses_folder_bound_collection(rw_server):
    url, _ = rw_server
    # 'ops' was indexed from a real folder; uploads must not write into it.
    body, ctype = _multipart({"collection": ("", b"ops"), "file": ("a.md", b"# a")})
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 409
    assert b"bound to folder" in resp


def test_upload_size_cap(rw_server):
    url, _ = rw_server
    big = b"x" * (26 * 1024 * 1024)
    body, ctype = _multipart({"collection": ("", b"notes"), "file": ("big.txt", big)})
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 400
    assert b"too large" in resp


def test_upload_preserves_exact_bytes(rw_server):
    """The multipart parser must not strip the payload's own trailing
    newlines — the stored sha256 must hash what the operator actually sent."""
    url, db_path = rw_server
    original = b"# Title\n\nbody line\n"
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("exact.md", original)}
    )
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 200, resp
    saved = (db_path.parent / "uploads" / "notes" / "exact.md").read_bytes()
    assert saved == original


def test_upload_accepts_filename_before_name_param(rw_server):
    """RFC 7578 fixes no parameter order; name= must not match filename=."""
    url, _ = rw_server
    boundary = "orderb"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; filename="ord.md"; name="file"\r\n\r\n'
        "# ordered\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="collection"\r\n\r\n'
        "notes\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    code, resp = _post(
        url + "/api/upload", body, f"multipart/form-data; boundary={boundary}"
    )
    assert code == 200, resp
    assert json.loads(resp)["saved"] == "ord.md"


def test_upload_refuses_silent_overwrite(rw_server):
    """Same sanitized name + different content = 409, never a silent replace;
    identical re-upload stays idempotent."""
    url, _ = rw_server
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("summary.md", b"# 2025 summary")}
    )
    assert _post(url + "/api/upload", body, ctype)[0] == 200
    body2, ctype2 = _multipart(
        {"collection": ("", b"notes"), "file": ("summary.md", b"# 2026 summary")}
    )
    code, resp = _post(url + "/api/upload", body2, ctype2)
    assert code == 409
    assert b"already exists" in resp
    # Identical bytes: fine (no-op re-index).
    assert _post(url + "/api/upload", body, ctype)[0] == 200


def test_upload_unindexable_file_is_removed(rw_server):
    """A file the extractor rejects must not stay on disk waiting to join
    the corpus on the next unrelated index run."""
    url, db_path = rw_server
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("broken.pdf", b"not a pdf at all")}
    )
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 400, resp
    assert not (db_path.parent / "uploads" / "notes" / "broken.pdf").exists()


def test_ip_literal_host_accepted(server):
    """Wide binds are reached by IP; an IP-literal Host is not a rebinding
    vector (rebinding needs a DNS name) and must be allowed."""
    req = urllib.request.Request(server + "/api/status")
    req.add_unredirected_header("Host", "100.101.102.103:8180")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200


def test_snippet_preserves_markdown_brackets(rw_server):
    """Match markers are non-printable \\x01/\\x02, so corpus text containing
    [markdown](links) reaches the client intact."""
    url, _ = rw_server
    original = "# Guide\n\nSee the [install guide](docs/install.md) then restart the service.\n"
    body, ctype = _multipart(
        {"collection": ("", b"notes"), "file": ("guide.md", original.encode())}
    )
    assert _post(url + "/api/upload", body, ctype)[0] == 200
    _, sbody = _get(url + "/api/search?q=restart%20service&collection=notes")
    hit = json.loads(sbody)["hits"][0]
    assert "[install guide](docs/install.md)" in hit["snippet"]
    assert chr(1) + "restart" + chr(2) in hit["snippet"]


def test_neighbor_error_does_not_kill_upload(rw_server):
    """Error attribution is delimited (filename + ':'), so a broken
    'a.md.pdf' beside a valid 'a.md' upload cannot get it deleted."""
    url, db_path = rw_server
    notes = db_path.parent / "uploads" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md.pdf").write_bytes(b"not a pdf")
    body, ctype = _multipart({"collection": ("", b"notes"), "file": ("a.md", b"# fine")})
    code, resp = _post(url + "/api/upload", body, ctype)
    assert code == 200, resp
    assert (notes / "a.md").is_file()


def test_concurrent_uploads_both_succeed(rw_server):
    """The write lock serializes uploads; neither may die on
    'database is locked'."""
    import concurrent.futures

    url, _ = rw_server

    def up(name: str):
        body, ctype = _multipart(
            {"collection": ("", b"notes"),
             "file": (name, f"# {name}\n\ncontent of {name}".encode())}
        )
        return _post(url + "/api/upload", body, ctype)[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        codes = list(ex.map(up, ["c1.md", "c2.md"]))
    assert codes == [200, 200]


# ---------------------------------------------------------------- feedback

def test_feedback_appends_log(rw_server):
    url, db_path = rw_server
    payload = {"q": "restart bridge", "path": "runbook.md", "seq": 0,
               "vote": "up", "collection": "ops"}
    code, resp = _post(
        url + "/api/feedback", json.dumps(payload).encode(), "application/json"
    )
    assert code == 200, resp
    log = db_path.parent / "feedback.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["q"] == "restart bridge"
    assert lines[-1]["vote"] == "up"
    assert lines[-1]["ts"]


def test_feedback_rejects_unknown_path(rw_server):
    """A forged path (e.g. 'e') would match nearly everything by substring
    once promoted into a fixture — only indexed paths are accepted."""
    url, _ = rw_server
    for bad_path in ("e", "nope.md", "runbook.md.fake"):
        payload = {"q": "who signs the audit", "path": bad_path, "vote": "up"}
        code, resp = _post(
            url + "/api/feedback", json.dumps(payload).encode(), "application/json"
        )
        assert code == 400, bad_path
        assert b"indexed document" in resp


def test_feedback_rejects_bad_vote(rw_server):
    url, _ = rw_server
    payload = {"q": "x", "path": "runbook.md", "vote": "sideways"}
    code, _ = _post(
        url + "/api/feedback", json.dumps(payload).encode(), "application/json"
    )
    assert code == 400


def test_search_writes_query_log(rw_server):
    url, db_path = rw_server
    _get(url + "/api/search?q=restart")
    log = db_path.parent / "query-log.jsonl"
    lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["q"] == "restart"
    assert lines[-1]["hits"] >= 1
    assert lines[-1]["latency_ms"] >= 0
