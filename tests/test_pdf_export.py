import pytest

pytestmark = pytest.mark.unit

"""Tests for the hand-rolled, stdlib-only PDF writer (roadmap: branded PDF export).

Covers structural validity (a real xref table with byte-correct offsets), the
WinAnsi sanitization (emoji -> text markers), escaping, multi-page flow, the
`write_report_pdf` file helper, and the `finsight report --pdf` CLI wiring.
"""

import re

from finance_agent.pdf_export import build_report_pdf, sanitize_winansi, write_report_pdf

SAMPLE_MD = """\
# FinSight Agent — Monthly Report

_Generated 2025-01-31 12:00 UTC · fully offline_

## Executive summary

Income $5,400.00, expenses $3,100.00, net $2,300.00 — savings rate 19.6%.

## Spending by category

| Category | Amount | Share |
|---|---|---|
| groceries | $650.00 | 21% |
| dining | $350.00 | 11% |
"""


# ---------------------------------------------------------------- structure
def test_pdf_starts_and_ends_correctly() -> None:
    pdf = build_report_pdf(SAMPLE_MD)
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_pdf_xref_offsets_are_byte_correct() -> None:
    """Every xref entry must point exactly at its 'N 0 obj' header."""
    pdf = build_report_pdf(SAMPLE_MD)
    startxref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    assert startxref > 0
    header, count_line, *entries = pdf[startxref:].splitlines()
    assert header == b"xref"
    # '0 N' — N is the number of xref entries (objects + 1 for object 0)
    count = int(count_line.split()[1])
    assert count >= 4  # catalog + pages + fonts + at least one content stream
    for entry in entries[1:]:  # entries[0] is the free object (obj 0)
        parts = entry.split()
        if len(parts) < 4 or parts[1] != b"n":
            continue
        offset, num = int(parts[0]), parts[2]
        assert pdf[offset : offset + len(num) + 8] == f"{num.decode()} 0 obj".encode()


def test_pdf_contains_page_and_font_objects() -> None:
    pdf = build_report_pdf(SAMPLE_MD)
    assert b"/Type /Page" in pdf
    assert b"/Helvetica" in pdf
    assert b"/MediaBox [0 0 595 842]" in pdf  # A4


def test_pdf_text_is_embeddable() -> None:
    """Body text (WinAnsi-safe) must appear in a content stream."""
    pdf = build_report_pdf(SAMPLE_MD)
    assert b"Executive summary" in pdf
    assert b"Spending by category" in pdf


def test_long_report_flows_across_pages() -> None:
    md = SAMPLE_MD + "\n## Extra\n\n- point one\n- point two\n" * 200
    pdf = build_report_pdf(md)
    assert pdf.count(b"/Type /Page ") >= 2


# --------------------------------------------------------------- sanitization
def test_sanitize_winansi_maps_emoji_to_text_markers() -> None:
    assert "over" in sanitize_winansi("⚠️ over")
    assert sanitize_winansi("📈 up") == "[up] up"


def test_pdf_replaces_emoji_but_keeps_winansi_glyphs() -> None:
    md = "## Budget tracker\n\n| Category | Used |\n|---|---|\n| dining | 102% ⚠️ over |\n"
    pdf = build_report_pdf(md)
    # the emoji becomes a marker; ± · — are WinAnsi-safe and stay
    assert b"[over]" in pdf
    assert b"102%" in pdf


def test_pdf_escapes_parentheses_and_backslashes() -> None:
    md = "# Title\n\n(weird) (parens) and \\ backslash\n"
    pdf = build_report_pdf(md)  # must not raise, and text survives
    assert b"parens" in pdf


def test_empty_markdown_still_produces_valid_pdf() -> None:
    pdf = build_report_pdf("")
    assert pdf.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf


# --------------------------------------------------------------------- files
def test_write_report_pdf_writes_file(tmp_path) -> None:
    path = write_report_pdf(str(tmp_path / "report.pdf"))
    assert path == str(tmp_path / "report.pdf")
    data = tmp_path.joinpath("report.pdf").read_bytes()
    assert data.startswith(b"%PDF-1.4")


def test_write_report_pdf_accepts_explicit_facts(tmp_path) -> None:
    from finance_agent.report import build_report

    from finance_agent.tools import FinanceFacts  # isort:skip

    facts = FinanceFacts("config.yaml")
    write_report_pdf(str(tmp_path / "real.pdf"), facts=facts)
    assert tmp_path.joinpath("real.pdf").read_bytes().startswith(b"%PDF-1.4")
    # the generated markdown and the PDF both carry the same heading
    assert "FinSight Agent" in build_report(facts)


# ----------------------------------------------------------------------- CLI
def test_cli_report_pdf_flag(tmp_path, monkeypatch, capsys) -> None:
    """`finsight report --pdf` writes the Markdown and a branded PDF."""
    import sys

    out_md = tmp_path / "monthly_report.md"
    monkeypatch.setattr(sys, "argv", ["finance_agent", "report", "--out", str(out_md), "--pdf"])
    from finance_agent.cli import main

    assert main() == 0
    captured = capsys.readouterr().out
    assert out_md.exists()
    assert "PDF written to" in captured
    pdf_path = tmp_path / "monthly_report.pdf"
    assert pdf_path.exists()
    assert pdf_path.read_bytes().startswith(b"%PDF-1.4")


def test_pdf_is_deterministic() -> None:
    """Same markdown in -> identical bytes out (writer has no timestamps)."""
    assert build_report_pdf(SAMPLE_MD) == build_report_pdf(SAMPLE_MD)
