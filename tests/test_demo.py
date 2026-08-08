"""The public demo's contract: one write survives the internet — asking —
and it is rate-limited; the API brain answers only from retrieved chunks
with citations copied verbatim from the search results."""

from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uplink import api_brain, asks  # noqa: E402
from uplink.indexer import index_folder  # noqa: E402
from uplink.ratelimit import RateLimiter  # noqa: E402
from uplink.webapp import make_handler  # noqa: E402

from test_webapp import _get, _post  # noqa: E402


# ------------------------------------------------------------- rate limiter

def test_five_asks_then_a_friendly_wait(tmp_path: Path):
    rl = RateLimiter(tmp_path / "rl.json")
    for i in range(5):
        ok, msg = rl.check("1.2.3.4", now=1000.0 + i)
        assert ok, f"ask {i + 1} should be allowed"
    ok, msg = rl.check("1.2.3.4", now=1010.0)
    assert not ok
    assert "5 demo questions" in msg
    assert "search stays unlimited" in msg.lower()
    # A different visitor is unaffected.
    ok, _ = rl.check("5.6.7.8", now=1010.0)
    assert ok


def test_questions_come_back_after_three_days(tmp_path: Path):
    rl = RateLimiter(tmp_path / "rl.json")
    for i in range(5):
        assert rl.check("1.2.3.4", now=1000.0 + i)[0]
    three_days = 3 * 24 * 3600
    assert not rl.check("1.2.3.4", now=1000.0 + three_days - 60)[0]
    assert rl.check("1.2.3.4", now=1000.0 + three_days + 60)[0]


def test_limit_survives_a_restart(tmp_path: Path):
    path = tmp_path / "rl.json"
    rl = RateLimiter(path)
    for i in range(5):
        assert rl.check("1.2.3.4", now=1000.0 + i)[0]
    fresh = RateLimiter(path)  # new process, same file
    assert not fresh.check("1.2.3.4", now=1010.0)[0]


# ---------------------------------------------------------------- API brain

@pytest.fixture
def demo_index(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "policy.md").write_text(
        "# Refund Policy\n\n## Windows\n\nRefunds are honoured within 30 days "
        "of purchase with a receipt.",
        encoding="utf-8",
    )
    (root / "shipping.md").write_text(
        "# Shipping\n\n## Times\n\nStandard shipping takes five business days.",
        encoding="utf-8",
    )
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="docs")
    asks_dir = tmp_path / "data" / "asks"
    asks_dir.mkdir()
    return db_path, asks_dir


def _answered(asks_dir: Path, ask_id: str) -> dict:
    return json.loads((asks_dir / f"{ask_id}.response.json").read_text("utf-8"))


def test_brain_answers_with_verbatim_citations(demo_index):
    db_path, asks_dir = demo_index
    req = asks.new_ask(asks_dir, "how long do refunds last", None)

    def fake_model(question, chunks_block):
        assert "refund" in question.lower()
        assert "[1]" in chunks_block  # chunks arrive numbered
        return {"answer": "Refunds are honoured within 30 days.",
                "chunk_numbers": [1]}

    api_brain.answer_one(db_path, asks_dir, req, call_model=fake_model)
    resp = _answered(asks_dir, req["id"])
    assert resp["state"] == "answered"
    assert resp["grounding_verified"] is True
    cite = resp["citations"][0]
    # Coordinates are copied from the index, never written by the model.
    assert cite["path"] == "policy.md"
    assert cite["collection"] == "docs"
    assert isinstance(cite["seq"], int)


def test_brain_refuses_when_nothing_is_selected(demo_index):
    db_path, asks_dir = demo_index
    req = asks.new_ask(asks_dir, "anything", None, docs=[])
    api_brain.answer_one(db_path, asks_dir, req,
                         call_model=lambda q, c: {"answer": "x", "chunk_numbers": []})
    resp = _answered(asks_dir, req["id"])
    assert resp["state"] == "error"
    assert "no sources" in resp["error"]


def test_brain_scope_travels_into_retrieval(demo_index):
    db_path, asks_dir = demo_index
    # Visitor selected ONLY the shipping doc; a refund question must not
    # surface the policy doc.
    req = asks.new_ask(asks_dir, "refunds shipping days", None,
                       docs=["docs/shipping.md"])
    seen = {}

    def fake_model(question, chunks_block):
        seen["chunks"] = chunks_block
        return {"answer": "Standard shipping takes five business days.",
                "chunk_numbers": [1]}

    api_brain.answer_one(db_path, asks_dir, req, call_model=fake_model)
    assert "policy.md" not in seen["chunks"]
    resp = _answered(asks_dir, req["id"])
    assert resp["state"] == "answered"
    assert resp["citations"][0]["path"] == "shipping.md"


def test_brain_says_so_when_documents_do_not_cover_it(demo_index):
    db_path, asks_dir = demo_index
    req = asks.new_ask(asks_dir, "zzqx vvbnq qqwwz", None)
    api_brain.answer_one(db_path, asks_dir, req,
                         call_model=lambda q, c: pytest.fail("must not call the API"))
    resp = _answered(asks_dir, req["id"])
    assert resp["state"] == "answered"
    assert resp["citations"] == []
    assert "don't seem to cover" in resp["answer"]


def test_answer_parser_splits_body_from_cited_line():
    """Plain-text replies end with a CITED line; the body is everything above
    it. No JSON to truncate, no envelope for reasoning to leak into."""
    from uplink.api_brain import _parse_answer
    reply = ("Apple's revenue grew.\n\n• 2021: $365B\n• 2025: $416B\n"
             "CITED: 1, 4, 5")
    out = _parse_answer(reply)
    assert out["chunk_numbers"] == [1, 4, 5]
    assert "CITED" not in out["answer"]
    assert out["answer"].startswith("Apple's revenue grew.")
    # A reply with no CITED line yields no citations, not an error.
    assert _parse_answer("Just an answer.")["chunk_numbers"] == []
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _parse_answer("CITED: 1")  # nothing above the line = empty body


def test_brain_api_failure_becomes_an_error_answer(demo_index):
    db_path, asks_dir = demo_index
    req = asks.new_ask(asks_dir, "how long do refunds last", None)

    def broken_model(question, chunks_block):
        raise ConnectionError("api down")

    api_brain.answer_one(db_path, asks_dir, req, call_model=broken_model)
    resp = _answered(asks_dir, req["id"])
    assert resp["state"] == "error"
    assert "unavailable" in resp["error"]


# ------------------------------------------------- public gating end to end

@pytest.fixture
def demo_server(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "runbook.md").write_text("# Runbook\n\nRestart nightly.", encoding="utf-8")
    db_path = tmp_path / "data" / "uplink.db"
    index_folder(root, db_path, collection="ops")
    (tmp_path / "data" / "asks").mkdir()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(db_path, None, writes_enabled=False,
                     fixtures_dir=tmp_path / "fixtures", demo_mode=True),
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()
    httpd.server_close()


def _ask(url: str, ip: str):
    return _post(url + "/api/ask", json.dumps({"q": "when do restarts run"}).encode(),
                 "application/json", extra={"X-Forwarded-For": ip})


def test_demo_allows_asks_but_nothing_else(demo_server):
    url, _ = demo_server
    status, body = _ask(url, "9.9.9.1")
    assert status == 200
    assert json.loads(body)["state"] == "pending"
    # Every other write stays locked.
    status, body = _post(url + "/api/feedback",
                         json.dumps({"q": "x", "path": "runbook.md", "vote": "up"}).encode(),
                         "application/json")
    assert status == 403
    status, _ = _post(url + "/api/notes", json.dumps({"title": "t", "body": "b"}).encode(),
                      "application/json")
    assert status == 403


def test_case_study_page_is_served(demo_server):
    url, _ = demo_server
    status, body = _get(url + "/case-study")
    assert status == 200
    text = body.decode("utf-8")
    # The recruiter-facing claims that must never silently vanish.
    assert "Case Study" in text or "Case study" in text
    assert "361" in text          # test count
    assert "confidence interval" in text
    assert "eval-history.jsonl" in text  # claims link to their evidence
    assert "Directed across the stack" in text  # the operator-role section
    assert "/performance" in text               # links to the detail page


def test_performance_page_is_served(demo_server):
    url, _ = demo_server
    status, body = _get(url + "/performance")
    assert status == 200
    text = body.decode("utf-8")
    assert "Keyword vs. hybrid" in text
    assert "93%" in text and "53%" in text       # the measured head-to-head
    assert "eval-history.jsonl" in text          # links to the raw evidence


def test_metrics_panel_renamed_from_studio():
    """The right panel is 'Metrics' (was 'Studio') and links to the detail
    page — pin both so a future edit can't silently revert the rename."""
    html = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "<h2>Metrics</h2>" in html
    assert "<h2>Studio</h2>" not in html
    assert 'href="/performance"' in html


def test_brain_answers_are_formatted_for_reading():
    """Answers open with a direct sentence and carry bullets — one paragraph
    walls of text are a product decision, not a model mood."""
    from uplink.api_brain import SYSTEM
    assert "bullet" in SYSTEM.lower()
    assert "•" in SYSTEM
    assert "One short sentence" in SYSTEM
    assert "never a paragraph" in SYSTEM


def test_reading_spreads_across_documents():
    """A question spanning years must pull passages from several filings —
    ranked search alone would hand the model one document."""
    from types import SimpleNamespace as Hit
    hits = [Hit(collection="apple", path=f"doc{i // 6}.xls", seq=i)
            for i in range(24)]  # 6 consecutive hits per document
    picked = api_brain._diversify(hits, per_doc=3, total=10)
    assert len(picked) == 10
    by_doc = {}
    for h in picked:
        by_doc[h.path] = by_doc.get(h.path, 0) + 1
    assert max(by_doc.values()) <= 3
    assert len(by_doc) >= 4  # spread over at least four documents


def test_demo_status_reports_asks_without_writes(demo_server):
    url, _ = demo_server
    _, body = _get(url + "/api/status")
    s = json.loads(body)
    assert s["writes"] is False
    assert s["asks"] is True
    assert s["demo"] is True


def test_sixth_ask_from_one_visitor_is_limited(demo_server):
    url, _ = demo_server
    for i in range(5):
        status, _ = _ask(url, "9.9.9.2")
        assert status == 200, f"ask {i + 1}"
    status, body = _ask(url, "9.9.9.2")
    assert status == 429
    assert "5 demo questions" in json.loads(body)["error"]
    # A different visitor still gets through.
    status, _ = _ask(url, "9.9.9.3")
    assert status == 200


def test_spoofed_forwarded_for_cannot_mint_new_allowances(demo_server):
    """The limit keys on the RIGHTMOST X-Forwarded-For hop — the one the proxy
    adds — so rotating the client-controlled left entries must NOT reset it."""
    url, _ = demo_server
    real = "7.7.7.7"
    for i in range(5):
        # Each request spoofs a different left value; the real IP is rightmost.
        status, _ = _post(
            url + "/api/ask", json.dumps({"q": "when do restarts run"}).encode(),
            "application/json",
            extra={"X-Forwarded-For": f"1.2.3.{i}, {real}"})
        assert status == 200, f"ask {i + 1}"
    status, body = _post(
        url + "/api/ask", json.dumps({"q": "when do restarts run"}).encode(),
        "application/json",
        extra={"X-Forwarded-For": f"9.9.9.9, {real}"})
    assert status == 429, "spoofing the left XFF entry bypassed the limit"


def test_public_metrics_do_not_echo_visitor_query_text(demo_server):
    url, _ = demo_server
    # A search that finds nothing gets logged; its text must not resurface.
    _get(url + "/api/search?q=zzzq-secret-sauce-xyz")
    _, body = _get(url + "/api/metrics")
    m = json.loads(body)
    assert "zzzq-secret-sauce-xyz" not in json.dumps(m)
    gaps = m.get("gaps", {})
    assert gaps.get("zero_hit") == []
    assert gaps.get("unknown_terms") == []


def test_rate_limiter_caps_tracked_keys(tmp_path: Path):
    rl = RateLimiter(tmp_path / "rl.json", max_keys=10)
    for i in range(25):
        rl.check(f"10.0.0.{i}", now=1000.0 + i)
    stored = json.loads((tmp_path / "rl.json").read_text("utf-8"))
    assert len(stored) <= 10
