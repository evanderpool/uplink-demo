"""Report generation: the script computes, the model narrates.

Three deterministic reports are computed straight from the index (read-only)
and rendered as self-contained HTML with inline SVG charts:

    health    what is indexed - documents, types, sizes, freshness
    quality   retrieval quality - live eval run + history over time
    activity  what changed recently - recency and age profile

An LLM (or a human) may supply a narrative paragraph per report; it is
injected into a clearly-marked block, HTML-escaped like all other untrusted
text. Reports are fully functional with no narrative and no LLM.

Determinism: given the same database, fixtures, history, and --now timestamp,
a report renders byte-identically. Tests rely on this.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .evaluate import run_eval
from .svgchart import bar_chart_h, line_chart

REPORT_KINDS = ("health", "quality", "activity")

_AGE_BUCKETS = (
    ("7 days or less", 7),
    ("7-30 days", 30),
    ("30-90 days", 90),
    ("older", None),
)


@dataclass
class ReportContext:
    db_path: Path
    out_dir: Path
    now: datetime
    fixtures: Path | None = None
    history: Path | None = None
    narrative: dict[str, str] | None = None  # kind -> narrative text


# --------------------------------------------------------------------------
# Data assembly (pure queries; no rendering)
# --------------------------------------------------------------------------

def health_data(db_path: Path) -> dict:
    conn = db.connect_ro(db_path)
    try:
        docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        total_bytes = conn.execute(
            "SELECT COALESCE(SUM(size), 0) n FROM documents"
        ).fetchone()["n"]
        by_type = [
            dict(r)
            for r in conn.execute(
                "SELECT filetype, COUNT(*) docs, "
                "(SELECT COUNT(*) FROM chunks c JOIN documents d2 ON d2.id = c.doc_id "
                " WHERE d2.filetype = d.filetype) chunks "
                "FROM documents d GROUP BY filetype ORDER BY docs DESC, filetype"
            )
        ]
        largest = [
            dict(r)
            for r in conn.execute(
                "SELECT d.path, d.filetype, COUNT(c.id) chunks "
                "FROM documents d JOIN chunks c ON c.doc_id = d.id "
                "GROUP BY d.id ORDER BY chunks DESC, d.path LIMIT 10"
            )
        ]
        last_indexed = conn.execute(
            "SELECT MAX(indexed_at) t FROM documents"
        ).fetchone()["t"]
        collections = db.list_collections(conn)
        roots = {
            r["key"].removeprefix("corpus_root:"): r["value"]
            for r in conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'corpus_root:%'"
            )
        }
    finally:
        conn.close()
    return {
        "documents": docs,
        "chunks": chunks,
        "total_bytes": total_bytes,
        "by_type": by_type,
        "largest": largest,
        "last_indexed": last_indexed,
        "collections": collections,
        "roots": roots,
    }


def quality_data(db_path: Path, fixtures: Path, history: Path | None, k: int = 5) -> dict:
    result = run_eval(db_path, fixtures, k=k)
    history_rows = load_history(history) if history and history.exists() else []
    # A "quality over time" line is only honest within one fixture set:
    # mixing runs of different fixtures (or corpora) would chart an
    # improvement that was never measured.
    history_rows = [
        r for r in history_rows
        if r.get("fixtures") == fixtures.name and r.get("db") == db_path.name
    ]
    return {
        "summary": result.to_dict(),
        "per_question": result.per_question,
        "history": history_rows,
    }


def activity_data(db_path: Path, now: datetime) -> dict:
    conn = db.connect_ro(db_path)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT path, filetype, mtime FROM documents ORDER BY mtime DESC, path"
            )
        ]
    finally:
        conn.close()
    recent = rows[:15]
    buckets = {label: 0 for label, _ in _AGE_BUCKETS}
    now_ts = now.timestamp()
    for r in rows:
        age_days = max(0.0, (now_ts - r["mtime"]) / 86400)
        for label, limit in _AGE_BUCKETS:
            if limit is None or age_days <= limit:
                buckets[label] += 1
                break
    return {"recent": recent, "age_buckets": buckets, "total": len(rows)}


# --------------------------------------------------------------------------
# Eval history log
# --------------------------------------------------------------------------

def append_history(
    history: Path,
    summary: dict,
    now: datetime,
    label: str = "",
    db: str | Path = "",
    fixtures: str | Path = "",
) -> None:
    """Append one eval run to the history log (metrics only, no corpus text).

    `db` and `fixtures` record run provenance (basenames only) so the quality
    chart can refuse to plot runs of different corpora/fixture sets as one line.
    """
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "label": label,
        "db": Path(db).name if db else "",
        "fixtures": Path(fixtures).name if fixtures else "",
        "questions": summary["questions"],
        "hit_at_1": summary["hit_at_1"],
        "hit_at_k": summary["hit_at_k"],
        "k": summary["k"],
        "mrr": summary["mrr"],
    }
    from .notes import append_line

    append_line(history, json.dumps(entry, ensure_ascii=True))


def load_history(history: Path) -> list[dict]:
    rows = []
    for line in history.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A torn write (crash mid-append) must not sink every future
            # quality report — same policy as the feedback log reader.
            continue
    return rows


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_all(ctx: ReportContext, kinds: list[str]) -> list[Path]:
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for kind in kinds:
        if kind == "health":
            page = _render_health(ctx, health_data(ctx.db_path))
        elif kind == "quality":
            if not ctx.fixtures:
                raise ValueError("quality report needs --fixtures")
            page = _render_quality(
                ctx, quality_data(ctx.db_path, ctx.fixtures, ctx.history)
            )
        elif kind == "activity":
            page = _render_activity(ctx, activity_data(ctx.db_path, ctx.now))
        else:
            raise ValueError(f"unknown report kind: {kind}")
        out = ctx.out_dir / f"{kind}.html"
        out.write_text(page, encoding="utf-8", newline="\n")
        written.append(out)
    return written


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _fmt_bytes(n: int) -> str:
    """Human-readable size: 512 B, 1.5 KB, 2.0 MB."""
    size = float(n)
    unit = "B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            break
        size /= 1024
    return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"


def _tiles(pairs: list[tuple[str, str]]) -> str:
    cells = "".join(
        f'<div class="tile"><div class="tile-v">{_e(v)}</div>'
        f'<div class="tile-l">{_e(label)}</div></div>'
        for label, v in pairs
    )
    return f'<div class="tiles">{cells}</div>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _narrative_block(ctx: ReportContext, kind: str) -> str:
    text = (ctx.narrative or {}).get(kind, "").strip()
    if not text:
        return ""
    paragraphs = "".join(f"<p>{_e(p)}</p>" for p in text.split("\n\n") if p.strip())
    return (
        '<section class="narrative"><h2>Summary</h2>'
        f"{paragraphs}"
        '<p class="narrative-note">Narrative written by the operator or an LLM '
        "assistant; all figures above are computed directly from the index.</p>"
        "</section>"
    )


def _render_health(ctx: ReportContext, data: dict) -> str:
    type_items = [(f".{r['filetype']}", float(r["docs"])) for r in data["by_type"]]
    chart_types = bar_chart_h(type_items, "Documents by file type")
    chart_chunky = bar_chart_h(
        [(r["path"], float(r["chunks"])) for r in data["largest"]],
        "Largest documents (chunks)",
    )
    body = (
        _tiles(
            [
                ("documents", str(data["documents"])),
                ("chunks", str(data["chunks"])),
                ("corpus size", _fmt_bytes(data["total_bytes"])),
                ("last indexed", (data["last_indexed"] or "never")[:16]),
            ]
        )
        + _narrative_block(ctx, "health")
        + f'<section class="chart">{chart_types}</section>'
        + f'<section class="chart">{chart_chunky}</section>'
        + "<h2>By collection</h2>"
        + _table(
            ["collection", "documents", "chunks", "source"],
            [
                [c["name"], c["documents"], c["chunks"], data["roots"].get(c["name"], "")]
                for c in data["collections"]
            ],
        )
        + "<h2>By file type</h2>"
        + _table(
            ["type", "documents", "chunks"],
            [[f".{r['filetype']}", r["docs"], r["chunks"]] for r in data["by_type"]],
        )
    )
    colls = data["collections"]
    if len(colls) == 1:
        subtitle = data["roots"].get(colls[0]["name"])
    elif colls:
        subtitle = f"{len(colls)} collections: " + ", ".join(c["name"] for c in colls)
    else:
        subtitle = None
    return _page(ctx, "Corpus Health", subtitle, body)


def _render_quality(ctx: ReportContext, data: dict) -> str:
    s = data["summary"]
    q_total = s["questions"] or 1
    history = data["history"]
    x_labels = [row["ts"][:10] for row in history]

    def _rate(r: dict, field: str) -> float | None:
        if not r.get("questions"):
            return None
        return r[field] / r["questions"]

    chart_hist = line_chart(
        [
            {"name": "hit@1", "values": [_rate(r, "hit_at_1") for r in history]},
            {
                "name": f"hit@{s['k']}",
                # A run evaluated at a different k is not a hit@{k} rate —
                # render it as an honest gap, never as a mixed metric.
                "values": [
                    _rate(r, "hit_at_k") if r.get("k") == s["k"] else None
                    for r in history
                ],
            },
            {"name": "MRR", "values": [r["mrr"] for r in history]},
        ],
        "Retrieval quality over time (0-1)",
        x_labels,
    )
    rows = []
    for pq in data["per_question"]:
        rank = pq["rank"]
        rows.append(
            [pq["q"], f"rank {rank}" if rank else "MISS", pq["top_path"]]
        )
    body = (
        _tiles(
            [
                ("questions", str(s["questions"])),
                ("hit@1", f"{s['hit_at_1'] / q_total:.0%}"),
                (f"hit@{s['k']}", f"{s['hit_at_k'] / q_total:.0%}"),
                ("MRR", f"{s['mrr']:.3f}"),
            ]
        )
        + _narrative_block(ctx, "quality")
        + f'<section class="chart">{chart_hist}</section>'
        + "<h2>Per-question results</h2>"
        + _table(["question", "result", "top hit"], rows)
    )
    return _page(ctx, "Retrieval Quality", None, body)


def _render_activity(ctx: ReportContext, data: dict) -> str:
    chart_age = bar_chart_h(
        [(label, float(n)) for label, n in data["age_buckets"].items()],
        "Document age (by last modification)",
    )
    rows = [
        [
            r["path"],
            f".{r['filetype']}",
            datetime.fromtimestamp(r["mtime"], tz=timezone.utc).strftime("%Y-%m-%d"),
        ]
        for r in data["recent"]
    ]
    body = (
        _tiles([("documents", str(data["total"])), ("shown", str(len(rows)))])
        + _narrative_block(ctx, "activity")
        + f'<section class="chart">{chart_age}</section>'
        + "<h2>Most recently modified</h2>"
        + _table(["path", "type", "modified (UTC)"], rows)
    )
    return _page(ctx, "Corpus Activity", None, body)


_CSS = """
:root { color-scheme: light dark; }
body {
  margin: 0; padding: 32px 20px 64px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--ink);
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) body {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70;
  }
}
:root[data-theme="dark"] body {
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #898781; --grid: #2c2c2a; --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70;
}
main { max-width: 760px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 10px; }
.meta { color: var(--muted); font-size: 12px; margin-bottom: 24px; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 16px; min-width: 110px;
}
.tile-v { font-size: 22px; font-weight: 650; }
.tile-l { font-size: 11px; color: var(--muted); margin-top: 2px; }
.chart {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px; margin: 14px 0; overflow-x: auto;
}
.narrative {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 4px 16px 8px; margin: 14px 0;
}
.narrative h2 { margin-top: 12px; }
.narrative-note { color: var(--muted); font-size: 11px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
th { text-align: left; color: var(--muted); font-weight: 600; }
th, td { padding: 6px 10px 6px 0; border-bottom: 1px solid var(--grid); }
td { color: var(--ink-2); overflow-wrap: anywhere; }
svg { display: block; }
circle:hover, path:hover { opacity: 0.85; }
"""


def _page(ctx: ReportContext, title: str, subtitle: str | None, body: str) -> str:
    sub = f'<div class="meta">{_e(subtitle)}</div>' if subtitle else ""
    stamp = ctx.now.strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Uplink - {_e(title)}</title>"
        f"<style>{_CSS}</style></head><body><main>"
        f"<h1>{_e(title)}</h1>"
        f'<div class="meta">Uplink report - generated {_e(stamp)}</div>'
        f"{sub}{body}"
        "</main></body></html>\n"
    )
