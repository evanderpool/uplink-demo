"""Every supported file type extracts real text; unsupported types are refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from uplink.extractors import SUPPORTED_EXTENSIONS, extract


def _all_text(extracted) -> str:
    return "\n".join(s.text for s in extracted.sections)


def test_markdown_sections_split_on_headings(corpus: Path):
    result = extract(corpus / "handbook.md")
    titles = [s.title for s in result.sections]
    assert "Backup Policy" in titles
    assert "Escalation" in titles
    assert result.title == "Operations Handbook"
    assert "nightly at 0200" in _all_text(result)


def test_txt(corpus: Path):
    result = extract(corpus / "notes.txt")
    assert "maintenance window" in _all_text(result)


def test_csv_keeps_header_and_rows(corpus: Path):
    result = extract(corpus / "inventory.csv")
    text = _all_text(result)
    assert "asset | owner | status" in text
    assert "firewall-01" in text
    assert result.sections[0].header.startswith("asset")


def test_tsv(corpus: Path):
    result = extract(corpus / "codes.tsv")
    assert "disk failure imminent" in _all_text(result)


def test_pdf(corpus: Path):
    result = extract(corpus / "briefing.pdf")
    text = _all_text(result)
    assert "migration freeze" in text
    assert result.sections[0].title == "Page 1"


def test_docx_headings_and_tables(corpus: Path):
    result = extract(corpus / "procedure.docx")
    titles = [s.title for s in result.sections]
    assert "Access Control Procedure" in titles or result.title == "Access Control Procedure"
    text = _all_text(result)
    assert "manager approval" in text
    assert "server room | level 3" in text


def test_xlsx_sheets_as_rows(corpus: Path):
    result = extract(corpus / "budget.xlsx")
    text = _all_text(result)
    assert "tailscale subscription" in text
    assert result.sections[0].title == "Sheet: Budget"


def test_xls_legacy_binary_rows(corpus: Path):
    result = extract(corpus / "vendors.xls")
    text = _all_text(result)
    assert "acme telecom" in text
    assert "2027" in text and "2027.0" not in text  # integer floats rendered clean
    assert result.sections[0].title.startswith("Sheet:")
    assert result.sections[0].header.startswith("vendor")


def test_unsupported_extension_raises(tmp_path: Path):
    weird = tmp_path / "photo.png"
    weird.write_bytes(b"not text")
    with pytest.raises(ValueError):
        extract(weird)


def test_supported_extensions_cover_requirements():
    for ext in (".md", ".txt", ".pdf", ".docx", ".xlsx", ".xls", ".csv", ".tsv"):
        assert ext in SUPPORTED_EXTENSIONS
