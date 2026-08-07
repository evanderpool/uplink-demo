"""Report generation: correctness, determinism, escaping, empty-index safety."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from uplink.indexer import index_folder
from uplink.report import (
    ReportContext,
    activity_data,
    append_history,
    health_data,
    load_history,
    quality_data,
    render_all,
)
from uplink.svgchart import bar_chart_h, line_chart

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def small_index(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "alpha.md").write_text(
        "# Alpha\n\n## Backups\n\nBackups run nightly at 0200.", encoding="utf-8"
    )
    (root / "beta.txt").write_text("Beta notes about the maintenance window.", encoding="utf-8")
    (root / "rows.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    db_path = tmp_path / "index.db"
    index_folder(root, db_path)
    return root, db_path, tmp_path


def _ctx(db_path: Path, tmp_path: Path, **kw) -> ReportContext:
    return ReportContext(
        db_path=db_path, out_dir=tmp_path / "reports", now=NOW, **kw
    )


def test_health_data_counts(small_index):
    _, db_path, _ = small_index
    data = health_data(db_path)
    assert data["documents"] == 3
    assert data["chunks"] >= 3
    types = {r["filetype"]: r["docs"] for r in data["by_type"]}
    assert types == {"md": 1, "txt": 1, "csv": 1}
    assert data["total_bytes"] > 0


def test_activity_buckets_sum_to_total(small_index):
    _, db_path, _ = small_index
    data = activity_data(db_path, NOW.replace(year=2027))
    assert sum(data["age_buckets"].values()) == data["total"] == 3


def test_reports_render_and_are_deterministic(small_index):
    _, db_path, tmp_path = small_index
    ctx = _ctx(db_path, tmp_path)
    paths = render_all(ctx, ["health", "activity"])
    first = [p.read_bytes() for p in paths]
    second = [p.read_bytes() for p in render_all(ctx, ["health", "activity"])]
    assert first == second  # same db + same now -> byte-identical


def test_quality_report_with_history(small_index, tmp_path: Path):
    _, db_path, _ = small_index
    fixtures = tmp_path / "g.jsonl"
    fixtures.write_text(
        json.dumps({"q": "when do backups run", "expect": ["alpha.md"]}) + "\n",
        encoding="utf-8",
    )
    history = tmp_path / "hist.jsonl"
    for i in range(3):
        append_history(
            history,
            {"questions": 1, "hit_at_1": 1, "hit_at_k": 1, "k": 5, "mrr": 0.5 + i * 0.1},
            NOW,
            label=f"run{i}",
            db=db_path,
            fixtures=fixtures,
        )
    data = quality_data(db_path, fixtures, history)
    assert data["summary"]["hit_at_k"] == 1
    assert data["per_question"][0]["rank"] == 1
    assert len(data["history"]) == 3
    assert load_history(history)[2]["mrr"] == 0.7

    ctx = _ctx(db_path, tmp_path, fixtures=fixtures, history=history)
    (page,) = render_all(ctx, ["quality"])
    html_text = page.read_text(encoding="utf-8")
    assert "hit@1" in html_text and "MRR" in html_text
    assert "when do backups run" in html_text


def test_corpus_content_is_html_escaped(tmp_path: Path):
    # '<' is illegal in Windows filenames, so the hostile-path vector here is
    # '&' + quotes (legal everywhere); unescaped they would corrupt the HTML.
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "a&b 'q'.md").write_text("# T\n\nbody text here", encoding="utf-8")
    db_path = tmp_path / "index.db"
    index_folder(root, db_path)
    ctx = _ctx(db_path, tmp_path)
    health, activity = render_all(ctx, ["health", "activity"])
    for page in (health, activity):
        text = page.read_text(encoding="utf-8")
        assert "a&amp;b" in text
        assert "a&b 'q'.md" not in text  # raw ampersand never reaches the page


def test_narrative_is_escaped_and_rendered(small_index, tmp_path: Path):
    _, db_path, _ = small_index
    ctx = _ctx(
        db_path, tmp_path,
        narrative={"health": "All good.\n\nNext: <b>phase 2</b>"},
    )
    (page,) = render_all(ctx, ["health"])
    text = page.read_text(encoding="utf-8")
    assert "All good." in text
    assert "<b>phase 2</b>" not in text
    assert "&lt;b&gt;phase 2&lt;/b&gt;" in text


def test_empty_index_reports_do_not_crash(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    db_path = tmp_path / "index.db"
    index_folder(root, db_path)
    ctx = _ctx(db_path, tmp_path)
    for p in render_all(ctx, ["health", "activity"]):
        assert p.exists()


def test_svgs_are_well_formed_xml():
    bar = bar_chart_h([("a & b", 3.0), ("<c>", 1.0)], 'Ampersands & <brackets> and "quotes"')
    ET.fromstring(bar)
    line = line_chart(
        [{"name": "hit@1", "values": [0.5, 0.7]}, {"name": "MRR", "values": [0.4, None]}],
        "Quality",
        ["2026-08-01", "2026-08-06"],
    )
    ET.fromstring(line)
    empty = line_chart([{"name": "x", "values": []}], "Empty", [])
    ET.fromstring(empty)


def test_line_chart_rejects_four_series():
    with pytest.raises(ValueError):
        line_chart(
            [{"name": str(i), "values": [0.1]} for i in range(4)], "t", ["x"]
        )


def test_cli_report_all_without_fixtures_skips_quality(small_index):
    root, db_path, tmp_path = small_index
    out = tmp_path / "cli-reports"
    proc = subprocess.run(
        [
            sys.executable, "-m", "uplink", "report", "all",
            "--db", str(db_path), "--out", str(out), "--now", "2026-08-06T12:00:00+00:00",
        ],
        capture_output=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert (out / "health.html").exists()
    assert (out / "activity.html").exists()
    assert not (out / "quality.html").exists()
    assert b"skipping quality" in proc.stdout


# ---- regression pins for the logic-review findings ----

def test_line_chart_none_gap_is_not_bridged():
    svg = line_chart(
        [{"name": "a", "values": [0.5, None, 0.9]}], "gap", ["d1", "d2", "d3"]
    )
    ET.fromstring(svg)
    # A bridged gap would produce one path with an L command spanning the
    # plot; a real gap over 3 points with the middle missing yields NO line
    # (two isolated points), only markers.
    assert '<path d="M' not in svg


def test_line_chart_end_labels_do_not_overlap():
    svg = line_chart(
        [
            {"name": "hit@1", "values": [0.67]},
            {"name": "hit@5", "values": [0.70]},
            {"name": "MRR", "values": [0.69]},
        ],
        "labels",
        ["2026-08-06"],
    )
    ns = "{http://www.w3.org/2000/svg}"
    root = ET.fromstring(svg)
    ys = sorted(
        float(el.get("y"))
        for el in root.iter(f"{ns}text")
        if el.get("fill") == "var(--ink-2)"
        and el.get("dominant-baseline") == "central"
    )
    assert len(ys) == 3
    assert all(b - a >= 13.9 for a, b in zip(ys, ys[1:])), ys


def test_bar_chart_zero_value_draws_no_bar():
    svg = bar_chart_h([("empty", 0.0), ("full", 5.0)], "zeros")
    ET.fromstring(svg)
    assert svg.count("<path") == 1  # only the non-zero bar


def test_quality_history_with_mismatched_k_gaps_not_mixes(small_index, tmp_path: Path):
    _, db_path, _ = small_index
    fixtures = tmp_path / "g.jsonl"
    fixtures.write_text(
        json.dumps({"q": "when do backups run", "expect": ["alpha.md"]}) + "\n",
        encoding="utf-8",
    )
    history = tmp_path / "hist.jsonl"
    append_history(history, {"questions": 1, "hit_at_1": 1, "hit_at_k": 1, "k": 10, "mrr": 1.0},
                   NOW, db=db_path, fixtures=fixtures)
    append_history(history, {"questions": 1, "hit_at_1": 1, "hit_at_k": 1, "k": 5, "mrr": 1.0},
                   NOW, db=db_path, fixtures=fixtures)
    ctx = _ctx(db_path, tmp_path, fixtures=fixtures, history=history)
    (page,) = render_all(ctx, ["quality"])
    text = page.read_text(encoding="utf-8")
    # k=10 run must not contribute a point to the hit@5 series:
    # exactly one hit@5 marker (the k=5 run).
    assert text.count("hit@5 - ") == 1


def test_cli_naive_now_is_treated_as_utc(small_index):
    _, db_path, tmp_path = small_index
    out = tmp_path / "naive-now"
    proc = subprocess.run(
        [
            sys.executable, "-m", "uplink", "report", "health",
            "--db", str(db_path), "--out", str(out), "--now", "2026-08-06T12:00:00",
        ],
        capture_output=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "generated 2026-08-06 12:00 UTC" in (out / "health.html").read_text(encoding="utf-8")
