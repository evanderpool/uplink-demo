"""Dependency-free SVG charts for Uplink reports.

Charts are emitted as inline SVG referencing CSS custom properties
(--s1..--s3 for series, --ink/--ink-2/--muted for text, --grid/--axis for
chrome), so one stylesheet themes every chart for light and dark mode.

Design rules applied: single-hue bars for magnitude; fixed series slot order
(never cycled); one y-axis; thin marks with rounded data-ends; values and
labels always in ink colors, never series colors; native <title> tooltips on
every mark; a legend whenever there are two or more series.
"""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

FONT = 'font-family="system-ui, -apple-system, Segoe UI, sans-serif"'


def _fmt(v: float) -> str:
    return f"{v:g}"


def bar_chart_h(
    items: list[tuple[str, float]],
    title: str,
    width: int = 640,
    value_fmt=None,
) -> str:
    """Horizontal bar chart: one series, one hue, direct value labels."""
    value_fmt = value_fmt or _fmt
    if not items:
        return _empty(title, width)

    bar_h, gap, label_w, value_w = 22, 8, 190, 64
    top = 34
    height = top + len(items) * (bar_h + gap) + 12
    plot_w = width - label_w - value_w - 24
    vmax = max(v for _, v in items) or 1.0

    parts = [_svg_open(width, height, title), _title(title)]
    y = top
    for label, value in items:
        w = max(2.0, plot_w * (value / vmax))
        cy = y + bar_h / 2
        parts.append(
            f'<text x="{label_w - 8}" y="{cy:.1f}" text-anchor="end" '
            f'dominant-baseline="central" fill="var(--ink-2)" font-size="12" {FONT}>'
            f"{escape(_trim(label, 28))}</text>"
        )
        # A zero value gets no bar at all — a sliver next to a "0" label
        # would draw data that is not there.
        if value > 0:
            # Rounded data-end (right), square baseline-end (left).
            r = min(4.0, w / 2)
            parts.append(
                f'<path d="M{label_w},{y} h{w - r:.1f} q{r},0 {r},{r} '
                f'v{bar_h - 2 * r} q0,{r} -{r},{r} h-{w - r:.1f} z" fill="var(--s1)">'
                f"<title>{escape(label)}: {escape(value_fmt(value))}</title></path>"
            )
        value_x = label_w + (w if value > 0 else 0) + 8
        parts.append(
            f'<text x="{value_x:.1f}" y="{cy:.1f}" '
            f'dominant-baseline="central" fill="var(--ink)" font-size="12" '
            f'font-variant-numeric="tabular-nums" {FONT}>{escape(value_fmt(value))}</text>'
        )
        y += bar_h + gap
    parts.append("</svg>")
    return "".join(parts)


def line_chart(
    series: list[dict],
    title: str,
    x_labels: list[str],
    width: int = 640,
    height: int = 300,
    y_max: float = 1.0,
) -> str:
    """Multi-series line chart on one 0..y_max axis.

    series: [{"name": str, "values": [float | None, ...]}] — values align with
    x_labels; None marks a gap. Series colors use slots --s1..--s3 in fixed
    order (three series maximum by design).
    """
    if len(series) > 3:
        raise ValueError("line_chart supports at most 3 series (palette slots 1-3)")
    if not x_labels or not any(s["values"] for s in series):
        return _empty(title, width)

    pad_l, pad_r, pad_t, pad_b = 46, 96, 40, 34
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(x_labels)

    def px(i: int) -> float:
        return pad_l + (plot_w * (i / (n - 1)) if n > 1 else plot_w / 2)

    def py(v: float) -> float:
        return pad_t + plot_h * (1 - min(v, y_max) / y_max)

    parts = [_svg_open(width, height, title), _title(title)]

    # Hairline grid + y ticks at quarters.
    for q in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = py(q * y_max)
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{pad_l + plot_w}" y2="{gy:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{gy:.1f}" text-anchor="end" '
            f'dominant-baseline="central" fill="var(--muted)" font-size="11" '
            f'font-variant-numeric="tabular-nums" {FONT}>{q * y_max:g}</text>'
        )

    # X labels: first, last, and evenly thinned middle ones.
    shown = _thin_indices(n, 6)
    for i in shown:
        parts.append(
            f'<text x="{px(i):.1f}" y="{height - 10}" text-anchor="middle" '
            f'fill="var(--muted)" font-size="11" {FONT}>{escape(_trim(x_labels[i], 12))}</text>'
        )

    label_anchors: list[tuple[float, float, str, str]] = []  # x, y, color, name
    for s_i, s in enumerate(series, start=1):
        # Values beyond the x-label range cannot be placed honestly — drop them.
        values = list(s["values"])[: len(x_labels)]
        indexed = [(i, v) for i, v in enumerate(values) if v is not None]
        if not indexed:
            continue
        color = f"var(--s{s_i})"
        # One path per run of CONSECUTIVE points: a None is a real gap in the
        # data and must never be bridged by a line implying a measurement.
        run: list[tuple[float, float]] = []
        prev_i = None
        runs: list[list[tuple[float, float]]] = []
        for i, v in indexed:
            if prev_i is not None and i != prev_i + 1 and run:
                runs.append(run)
                run = []
            run.append((px(i), py(v)))
            prev_i = i
        if run:
            runs.append(run)
        for pts in runs:
            if len(pts) > 1:
                d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
                parts.append(
                    f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>'
                )
        for (i, v) in indexed:
            parts.append(
                f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" fill="{color}" '
                f'stroke="var(--surface)" stroke-width="2">'
                f"<title>{escape(s['name'])} - {escape(x_labels[i])}: {v:g}</title></circle>"
            )
        last_i, last_v = indexed[-1]
        label_anchors.append((px(last_i), py(last_v), color, s["name"]))

    # Direct labels, de-overlapped: series routinely end near the same value
    # (MRR sits between hit@1 and hit@k by construction), so push labels apart
    # to at least 14px vertical separation, clamped to the plot area.
    label_anchors.sort(key=lambda a: a[1])
    min_gap = 14.0
    ys = [a[1] for a in label_anchors]
    for j in range(1, len(ys)):
        ys[j] = max(ys[j], ys[j - 1] + min_gap)
    overflow = ys[-1] - (pad_t + plot_h) if ys else 0
    if overflow > 0:
        ys = [y - overflow for y in ys]
    for (lx, _, color, name), ly in zip(label_anchors, ys):
        parts.append(
            f'<circle cx="{lx + 12:.1f}" cy="{ly:.1f}" r="4" fill="{color}"/>'
            f'<text x="{lx + 20:.1f}" y="{ly:.1f}" dominant-baseline="central" '
            f'fill="var(--ink-2)" font-size="11" {FONT}>{escape(name)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _svg_open(width: int, height: int, title: str) -> str:
    # quoteattr, not escape: attribute values need '"' escaped too.
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label={quoteattr(title)} xmlns="http://www.w3.org/2000/svg">'
    )


def _title(title: str) -> str:
    return (
        f'<text x="0" y="18" fill="var(--ink)" font-size="13" font-weight="600" '
        f"{FONT}>{escape(title)}</text>"
    )


def _empty(title: str, width: int) -> str:
    return (
        _svg_open(width, 80, title)
        + _title(title)
        + f'<text x="0" y="52" fill="var(--muted)" font-size="12" {FONT}>no data yet</text></svg>'
    )


def _trim(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _thin_indices(n: int, max_labels: int) -> list[int]:
    if n <= max_labels:
        return list(range(n))
    step = (n - 1) / (max_labels - 1)
    return sorted({round(i * step) for i in range(max_labels)})
