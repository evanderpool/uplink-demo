"""Section text -> overlapping chunks sized for retrieval.

Prose is split at paragraph (then line) boundaries near a target size, with a
small overlap so answers straddling a boundary stay findable. Tabular text
(CSV rows, spreadsheet rows) gets the header line re-prepended to every chunk
so each chunk remains self-describing.
"""

from __future__ import annotations

TARGET_CHARS = 1600
OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 40


def chunk_section(text: str, header: str = "") -> list[str]:
    """Split one section's text into chunk strings."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= TARGET_CHARS:
        return [_with_header(text, header)]

    pieces = _split_blocks(text)
    chunks: list[str] = []
    buf = ""
    for piece in pieces:
        if buf and len(buf) + len(piece) + 1 > TARGET_CHARS:
            chunks.append(buf)
            # Carry a tail of the previous chunk forward as overlap.
            buf = buf[-OVERLAP_CHARS:].lstrip() if OVERLAP_CHARS else ""
        buf = f"{buf}\n{piece}".strip() if buf else piece
        # A single oversized block (giant paragraph / long row run) is split hard.
        while len(buf) > TARGET_CHARS * 2:
            chunks.append(buf[:TARGET_CHARS])
            buf = buf[TARGET_CHARS - OVERLAP_CHARS:]
    if len(buf) >= MIN_CHUNK_CHARS or not chunks:
        chunks.append(buf)
    return [_with_header(c, header) for c in chunks if c.strip()]


def _with_header(chunk: str, header: str) -> str:
    if header and not chunk.startswith(header):
        return f"{header}\n{chunk}"
    return chunk


def _split_blocks(text: str) -> list[str]:
    """Prefer paragraph boundaries; fall back to lines for wall-of-text input."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    blocks: list[str] = []
    for p in paragraphs:
        if len(p) > TARGET_CHARS:
            blocks.extend(line for line in p.splitlines() if line.strip())
        else:
            blocks.append(p)
    return blocks
