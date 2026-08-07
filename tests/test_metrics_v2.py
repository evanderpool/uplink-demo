"""Confidence intervals, regression deltas, live failing questions, the
answer-lifecycle metrics, and answer-level feedback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from uplink import metrics
from uplink.feedback import append_jsonl, promote


# ------------------------------------------------------- confidence intervals

def test_wilson_widens_as_the_sample_shrinks():
    """The whole point: 12/13 is a much weaker claim than 1200/1300, and the
    interval has to say so."""
    small = metrics.wilson(12, 13)
    large = metrics.wilson(1200, 1300)
    assert small[0] < large[0]
    assert (small[1] - small[0]) > (large[1] - large[0]) * 3


def test_wilson_stays_inside_zero_and_one():
    """The normal approximation runs off the end of the scale at the extremes;
    Wilson must not."""
    for successes, total in ((0, 5), (5, 5), (1, 1), (0, 1)):
        lo, hi = metrics.wilson(successes, total)
        assert 0.0 <= lo <= hi <= 1.0


def test_wilson_undefined_without_data():
    assert metrics.wilson(0, 0) is None


def test_accuracy_reports_an_interval_with_the_rate(tmp_path: Path):
    hist = tmp_path / "h.jsonl"
    hist.write_text(json.dumps({
        "ts": "2026-08-06T00:00:00+00:00", "label": "baseline", "db": "uplink.db",
        "fixtures": "industry-golden.jsonl", "questions": 13, "hit_at_1": 12,
        "hit_at_k": 12, "k": 5, "mrr": 0.923,
    }) + "\n", encoding="utf-8")
    acc = metrics.accuracy(hist, "uplink.db")
    assert acc["hit_at_1"] == round(12 / 13, 3)
    lo, hi = acc["hit_at_1_ci"]
    assert lo < acc["hit_at_1"] < hi
    assert lo < 0.8, "13 questions cannot support a tight lower bound"


# ------------------------------------------------------------------- deltas

def _hist(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "h.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return p


def test_delta_reports_improvement_between_runs(tmp_path: Path):
    hist = _hist(tmp_path, [
        {"ts": "t1", "label": "raw", "db": "x.db", "fixtures": "g.jsonl",
         "questions": 18, "hit_at_1": 8, "hit_at_k": 12, "k": 5, "mrr": 0.532},
        {"ts": "t2", "label": "stopwords", "db": "x.db", "fixtures": "g.jsonl",
         "questions": 18, "hit_at_1": 12, "hit_at_k": 16, "k": 5, "mrr": 0.769},
    ])
    d = metrics.accuracy(hist, "x.db")["delta"]
    assert d["since"] == "raw"
    assert d["hit_at_1"] == round(12 / 18 - 8 / 18, 3)
    assert d["mrr"] == 0.237
    assert d["comparable"] is True


def test_delta_flags_incomparable_fixture_sets(tmp_path: Path):
    """Comparing a score against a run of DIFFERENT questions is meaningless
    and must be labelled as such rather than charted as progress."""
    hist = _hist(tmp_path, [
        {"ts": "t1", "label": "ops", "db": "x.db", "fixtures": "golden.jsonl",
         "questions": 18, "hit_at_1": 12, "hit_at_k": 16, "k": 5, "mrr": 0.769},
        {"ts": "t2", "label": "industry", "db": "x.db", "fixtures": "industry.jsonl",
         "questions": 13, "hit_at_1": 12, "hit_at_k": 12, "k": 5, "mrr": 0.923},
    ])
    assert metrics.accuracy(hist, "x.db")["delta"]["comparable"] is False


def test_no_delta_from_a_single_run(tmp_path: Path):
    hist = _hist(tmp_path, [
        {"ts": "t", "db": "x.db", "fixtures": "g.jsonl", "questions": 4,
         "hit_at_1": 4, "hit_at_k": 4, "k": 5, "mrr": 1.0},
    ])
    assert metrics.accuracy(hist, "x.db")["delta"] is None


# -------------------------------------------------------------- live scoring

@pytest.fixture
def scored(tmp_path: Path):
    from uplink.indexer import index_folder

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "backups.md").write_text(
        "# Backups\n\n## Nightly\n\nBackups run nightly at 0200 and are kept 30 days.",
        encoding="utf-8")
    (root / "escalation.md").write_text(
        "# Escalation\n\n## Paging\n\nSeverity-one incidents page the on-call engineer.",
        encoding="utf-8")
    dbp = tmp_path / "u.db"
    index_folder(root, dbp, collection="ops")

    fixtures = tmp_path / "g.jsonl"
    fixtures.write_text(
        json.dumps({"q": "when do backups run", "expect": ["backups.md"]}) + "\n" +
        json.dumps({"q": "who gets paged for severity one", "expect": ["escalation.md"]}) + "\n" +
        json.dumps({"q": "zzz nonexistent topic entirely", "expect": ["nothing.md"]}) + "\n",
        encoding="utf-8")
    return dbp, fixtures


def test_live_accuracy_names_the_failing_questions(scored):
    """A score is a number; a named failing question is a to-do item."""
    dbp, fixtures = scored
    live = metrics.live_accuracy(dbp, fixtures, k=5)
    assert live["available"] is True
    assert live["questions"] == 3
    assert [f["q"] for f in live["failing"]] == ["zzz nonexistent topic entirely"]
    assert live["hit_at_1"] == round(2 / 3, 3)


def test_live_accuracy_missing_fixtures_is_explained(tmp_path: Path):
    out = metrics.live_accuracy(tmp_path / "u.db", tmp_path / "nope.jsonl")
    assert out["available"] is False
    assert "fixture" in out["reason"]


def test_live_accuracy_refuses_oversized_fixture_sets(scored, monkeypatch):
    """Live scoring runs on every page load; it must stay bounded."""
    dbp, fixtures = scored
    monkeypatch.setattr(metrics, "MAX_LIVE_EVAL_QUESTIONS", 2)
    out = metrics.live_accuracy(dbp, fixtures)
    assert out["available"] is False
    assert "limit" in out["reason"]


def test_live_accuracy_survives_a_broken_database(tmp_path: Path):
    bad = tmp_path / "notadb.db"
    bad.write_bytes(b"definitely not sqlite")
    fixtures = tmp_path / "g.jsonl"
    fixtures.write_text(json.dumps({"q": "x", "expect": ["y"]}) + "\n", encoding="utf-8")
    assert metrics.live_accuracy(bad, fixtures)["available"] is False


# ------------------------------------------------------------ answer metrics

def _ask(asks: Path, ask_id: str, q: str, asked: str, answered: str | None,
         citations: list[dict] | None = None) -> None:
    asks.mkdir(parents=True, exist_ok=True)
    (asks / f"{ask_id}.request.json").write_text(
        json.dumps({"id": ask_id, "ts": asked, "q": q}), encoding="utf-8")
    if answered:
        (asks / f"{ask_id}.response.json").write_text(json.dumps({
            "id": ask_id, "ts": answered, "state": "answered",
            "answer": "text", "citations": citations or [],
        }), encoding="utf-8")


def test_answer_metrics_measure_the_generative_side(tmp_path: Path):
    asks = tmp_path / "asks"
    _ask(asks, "a" * 12, "q one", "2026-08-07T00:00:00+00:00",
         "2026-08-07T00:00:30+00:00", [{"path": "one.md"}, {"path": "two.md"}])
    _ask(asks, "b" * 12, "q two", "2026-08-07T00:01:00+00:00",
         "2026-08-07T00:02:00+00:00", [{"path": "three.md"}])
    _ask(asks, "c" * 12, "q three", "2026-08-07T00:03:00+00:00", None)  # still pending

    qlog = tmp_path / "query-log.jsonl"
    # one.md opened AFTER its answer -> that answer counts as verified
    append_jsonl(qlog, {"ts": "2026-08-07T00:00:45+00:00", "kind": "doc", "path": "one.md"})
    # three.md opened BEFORE its answer -> not verification of that answer
    append_jsonl(qlog, {"ts": "2026-08-07T00:00:10+00:00", "kind": "doc", "path": "three.md"})

    notes = tmp_path / "notes.jsonl"
    append_jsonl(notes, {"id": "d" * 12, "title": "q one", "body": "b"})

    a = metrics.answers(asks, qlog, notes)
    assert a["answered"] == 2, "a pending ask is not an answer"
    assert a["median_seconds"] in (30.0, 60.0)
    assert a["avg_citations"] == 1.5
    assert a["verified"] == 1
    assert a["verification_rate"] == 0.5
    assert a["saved_as_notes"] == 1
    assert a["save_rate"] == 0.5


def test_uncited_answers_are_counted_separately(tmp_path: Path):
    """An answer with no citations is ungrounded — it must not hide inside
    an average."""
    asks = tmp_path / "asks"
    _ask(asks, "a" * 12, "q", "2026-08-07T00:00:00+00:00",
         "2026-08-07T00:00:10+00:00", [])
    a = metrics.answers(asks, tmp_path / "q.jsonl", None)
    assert a["answered"] == 1
    assert a["uncited_answers"] == 1
    assert a["verification_rate"] == 0.0


def test_answer_metrics_on_an_empty_queue(tmp_path: Path):
    assert metrics.answers(tmp_path / "nope", tmp_path / "q.jsonl")["answered"] == 0


# --------------------------------------------------- answer-level feedback

def test_answer_upvote_promotes_all_cited_documents(tmp_path: Path):
    """'This answer was right' means every document it cited was acceptable,
    so the fixture it becomes should accept any of them."""
    fb = tmp_path / "feedback.jsonl"
    out = tmp_path / "promoted.jsonl"
    append_jsonl(fb, {
        "ts": "t", "q": "what are the phases", "path": "nist.pdf",
        "paths": ["nist.pdf", "nist.pdf", "other.pdf"],
        "kind": "answer", "vote": "up", "collection": "tech",
    })
    added, _ = promote(fb, out)
    assert added == 1
    fixture = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert fixture["expect"] == ["nist.pdf", "other.pdf"], "deduped, order kept"
    assert fixture["kind"] == "answer"


def test_hit_upvote_still_promotes_one_path(tmp_path: Path):
    fb = tmp_path / "feedback.jsonl"
    out = tmp_path / "promoted.jsonl"
    append_jsonl(fb, {"ts": "t", "q": "backups", "path": "b.md", "vote": "up"})
    promote(fb, out)
    fixture = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert fixture["expect"] == ["b.md"]
    assert fixture["kind"] == "hit"


def test_answer_downvote_is_never_promoted(tmp_path: Path):
    fb = tmp_path / "feedback.jsonl"
    out = tmp_path / "promoted.jsonl"
    append_jsonl(fb, {"ts": "t", "q": "q", "path": "a.md", "paths": ["a.md", "b.md"],
                      "kind": "answer", "vote": "down"})
    added, _ = promote(fb, out)
    assert added == 0


# ---------------------------------------------------------- the endpoint

def test_metrics_endpoint_carries_every_block(tmp_path: Path):
    """The panel is only as honest as the payload behind it."""
    import json as _json

    from test_webapp import _get, _make_server

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        _, body = _get(url + "/api/metrics")
        m = _json.loads(body)
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert set(m) >= {"health", "accuracy", "live", "performance", "answers", "feedback"}
    # No eval history in an isolated server: both accuracy views say so
    # rather than inventing a number.
    assert m["accuracy"]["available"] is False
    assert m["live"]["available"] is False
    assert "reason" in m["live"]
    assert m["answers"]["answered"] == 0


def test_answer_vote_records_all_paths_over_http(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        payload = {
            "q": "how do restarts work", "path": "runbook.md",
            "paths": ["runbook.md", "runbook.md"], "kind": "answer",
            "vote": "up", "collection": "ops",
        }
        code, resp = _post(url + "/api/feedback",
                           _json.dumps(payload).encode(), "application/json")
        assert code == 200, resp
    finally:
        httpd.shutdown()
        httpd.server_close()

    entry = _json.loads(
        (db_path.parent / "feedback.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert entry["kind"] == "answer"
    assert entry["path"] == "runbook.md"
    # Duplicates collapse; a single-path answer carries no redundant list.
    assert "paths" not in entry


def test_answer_vote_still_requires_an_indexed_path(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, _ = _make_server(tmp_path, writes_enabled=True)
    try:
        code, resp = _post(
            url + "/api/feedback",
            _json.dumps({"q": "q", "path": "e", "kind": "answer", "vote": "up"}).encode(),
            "application/json",
        )
        assert code == 400
        assert b"indexed document" in resp
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------ retrieval gaps

@pytest.fixture
def gapped(tmp_path: Path):
    from uplink import db as _db
    from uplink.indexer import index_folder

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "leave.md").write_text(
        "# Leave\n\n## Paid time off\n\nStaff accrue paid time off each month.",
        encoding="utf-8")
    dbp = tmp_path / "u.db"
    index_folder(root, dbp, collection="hr")
    return dbp, _db


def test_unknown_term_detector_names_the_missing_word(gapped, tmp_path: Path):
    """The corpus says 'paid time off'; the user asked for 'PTO'. Naming the
    term that matches nothing is the actionable half of the loop."""
    dbp, _db = gapped
    log = tmp_path / "query-log.jsonl"
    append_jsonl(log, {"ts": "t1", "q": "what is our PTO accrue rate", "hits": 0})

    conn = _db.connect_ro(dbp)
    try:
        g = metrics.gaps(conn, log)
    finally:
        conn.close()

    assert [z["q"] for z in g["zero_hit"]] == ["what is our PTO accrue rate"]
    terms = [u["term"].lower() for u in g["unknown_terms"]]
    assert "pto" in terms, "the term the corpus never uses must be named"
    # Words the corpus DOES contain are not reported as missing — otherwise
    # every failed query would dump its whole vocabulary here.
    assert "accrue" not in terms


def test_gaps_ignore_successful_and_non_search_entries(gapped, tmp_path: Path):
    dbp, _db = gapped
    log = tmp_path / "query-log.jsonl"
    append_jsonl(log, {"ts": "t1", "q": "paid time off", "hits": 3})
    append_jsonl(log, {"ts": "t2", "kind": "doc", "path": "leave.md"})
    conn = _db.connect_ro(dbp)
    try:
        g = metrics.gaps(conn, log)
    finally:
        conn.close()
    assert g["zero_hit"] == []
    assert g["unknown_terms"] == []


def test_repeated_failures_rank_first(gapped, tmp_path: Path):
    """A question asked three times and never answered matters more than one
    asked once."""
    dbp, _db = gapped
    log = tmp_path / "query-log.jsonl"
    for _ in range(3):
        append_jsonl(log, {"ts": "t", "q": "zzz repeated miss", "hits": 0})
    append_jsonl(log, {"ts": "t", "q": "yyy single miss", "hits": 0})
    conn = _db.connect_ro(dbp)
    try:
        g = metrics.gaps(conn, log)
    finally:
        conn.close()
    assert g["zero_hit"][0]["q"] == "zzz repeated miss"
    assert g["zero_hit"][0]["count"] == 3


def test_gaps_skip_stopwords_and_short_tokens(gapped, tmp_path: Path):
    """'the' matching nothing is noise, not a finding."""
    dbp, _db = gapped
    log = tmp_path / "query-log.jsonl"
    append_jsonl(log, {"ts": "t", "q": "is the qq zzzunknownword", "hits": 0})
    conn = _db.connect_ro(dbp)
    try:
        terms = [u["term"].lower() for u in metrics.gaps(conn, log)["unknown_terms"]]
    finally:
        conn.close()
    assert "zzzunknownword" in terms
    assert "is" not in terms and "the" not in terms and "qq" not in terms


def test_promote_endpoint_closes_the_loop(tmp_path: Path):
    import json as _json

    from test_webapp import _make_server, _post

    httpd, url, db_path = _make_server(tmp_path, writes_enabled=True)
    try:
        _post(url + "/api/feedback",
              _json.dumps({"q": "how do restarts work", "path": "runbook.md",
                           "vote": "up"}).encode(), "application/json")
        code, resp = _post(url + "/api/promote", b"{}", "application/json")
        assert code == 200, resp
        assert _json.loads(resp)["added"] == 1

        # Idempotent: promoting again adds nothing.
        code, resp = _post(url + "/api/promote", b"{}", "application/json")
        assert _json.loads(resp)["added"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()

    fixture = _json.loads(
        (tmp_path / "fixtures" / "promoted.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert fixture["expect"] == ["runbook.md"]


def test_promote_refused_when_not_loopback(tmp_path: Path):
    from test_webapp import _make_server, _post

    httpd, url, _ = _make_server(tmp_path, writes_enabled=False)
    try:
        code, _ = _post(url + "/api/promote", b"{}", "application/json")
        assert code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
