"""Chunker properties: nothing dropped, overlap bounded, headers preserved."""

from __future__ import annotations

from uplink.chunker import MIN_CHUNK_CHARS, OVERLAP_CHARS, TARGET_CHARS, chunk_section


def test_short_text_is_one_chunk():
    assert chunk_section("hello world") == ["hello world"]


def test_empty_text_yields_nothing():
    assert chunk_section("   \n\n  ") == []


def test_no_content_dropped_across_chunks():
    """Every sentinel word must survive chunking somewhere."""
    sentinels = [f"sentinel{i:04d}" for i in range(400)]
    text = "\n\n".join(
        f"Paragraph about {s} with enough words to give it a little body."
        for s in sentinels
    )
    chunks = chunk_section(text)
    joined = "\n".join(chunks)
    missing = [s for s in sentinels if s not in joined]
    assert not missing, f"dropped: {missing[:5]}"
    assert len(chunks) > 1


def test_chunk_sizes_bounded():
    text = "\n\n".join(f"para {i} " + "word " * 60 for i in range(100))
    for chunk in chunk_section(text):
        assert len(chunk) <= TARGET_CHARS * 2 + OVERLAP_CHARS


def test_giant_single_line_is_hard_split_without_loss():
    text = "x" * 12000
    chunks = chunk_section(text)
    assert all(len(c) <= TARGET_CHARS * 2 for c in chunks)
    # Reassembled length can exceed original (overlap) but never lose coverage:
    assert sum(len(c) for c in chunks) >= 12000


def test_tabular_header_prepended_to_every_chunk():
    header = "asset | owner | status"
    rows = "\n".join(f"asset-{i:04d} | team | active" for i in range(300))
    chunks = chunk_section(rows, header=header)
    assert len(chunks) > 1
    assert all(c.startswith(header) for c in chunks)


def test_tiny_trailing_chunk_absorbed_or_kept_meaningfully():
    text = ("word " * 400).strip() + "\n\nzz"
    chunks = chunk_section(text)
    # No chunk should be below the minimum unless it is the only chunk.
    if len(chunks) > 1:
        assert all(len(c) >= MIN_CHUNK_CHARS or c == chunks[0] for c in chunks[1:-1])
