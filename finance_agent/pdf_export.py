"""Branded PDF export for the monthly report — hand-rolled, stdlib-only.

Zero new dependencies (matching the project's ethos: the digest is stdlib-only,
storage is stdlib ``sqlite3``, and heavy deps like xgboost were removed).  This
module writes a valid, paginated A4 PDF directly — objects, xref table, and
content streams — using the PDF standard-14 base fonts (Helvetica family), so
no font files are embedded and any viewer renders it.

Layout: A4 portrait, navy brand band on every page, accent-rule section
headings, shaded table headers with alternating row tint, and a footer with
"Page X of Y" plus the generation timestamp.

Text measurement, sanitization, and the layout engine live in sibling modules
(``pdf_text``, ``pdf_layout``) — this file handles only PDF object assembly
and the public API.
"""

from __future__ import annotations

import os
from typing import Any

from finance_agent.pdf_layout import _Layout
from finance_agent.pdf_text import (
    PAGE_H,
    PAGE_W,
    _parse_blocks,  # re-exported for backward compatibility
    sanitize_winansi,  # re-exported for backward compatibility
)

# ---------------------------------------------------------------------------
# PDF emission (objects, xref, trailer)
# ---------------------------------------------------------------------------


def _content_stream_bytes(cmds: list[bytes]) -> bytes:
    return b"".join(cmds)


def build_report_pdf(markdown: str) -> bytes:
    """Render ``markdown`` (the report body) to a complete, valid PDF (bytes)."""
    blocks = _parse_blocks(markdown)
    if not blocks:
        blocks = [("para", "No report content.")]

    # Pull the generation timestamp out of the report's subtitle line so the
    # footer and header agree with what the report claims.
    generation = ""
    for kind, text in blocks:
        if kind == "subtitle":
            generation = str(text).replace("Generated ", "")
            break

    lay = _Layout(generation)

    for kind, data in blocks:
        if kind == "title":
            lay.title(str(data))
        elif kind == "subtitle":
            pass  # already consumed for generation timestamp
        elif kind == "h2":
            lay.heading(str(data))
        elif kind == "para":
            lay.para(str(data))
        elif kind == "bullet":
            lay.bullet(str(data))
        elif kind == "table":
            lay.table(data)
        elif kind == "rule":
            lay.hrule()
        elif kind == "italic":
            lay.italic(str(data))

    return _assemble(lay.pages)


def _assemble(pages: list[list[bytes]]) -> bytes:
    """Turn a list of page command-lists into a complete PDF byte-string."""
    body_parts: list[bytes] = []
    offsets: list[int] = []
    objects: list[bytes] = []

    def _add(raw: bytes) -> int:
        """Register a PDF object and return its byte-offset."""
        idx = len(offsets)
        offsets.append(len(b"".join(objects)))
        objects.append(raw)
        return idx

    # Object 1 — Catalog
    _add(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # Object 2 — Pages
    kids = " ".join(f"{3 + i} 0 R" for i in range(len(pages)))
    _add(f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>\nendobj\n".encode())

    # Objects 3..N+2 — Page objects
    for cmds in pages:
        stream = _content_stream_bytes(cmds)
        _add(
            f"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Contents {len(objects) + 2} 0 R "
            f"/Resources << /Font << /F1 4 0 R /F2 5 0 R /F3 6 0 R >> >> >>\n"
            f"endobj\n".encode()
        )
        # Content stream (numbered sequentially after pages)
        _add(
            f"{len(objects) + 1} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream\nendobj\n"
        )

    # Standard-14 font definitions (objects are numbered dynamically)
    for fname, style in [("F1", ""), ("F2", "/Subtype /Type1"), ("F3", "/Subtype /Type1")]:
        subtype = "/Subtype /Type1"
        extra = ""
        if fname == "F2":
            extra = " /BaseFont /Helvetica-Bold"
        elif fname == "F3":
            extra = " /BaseFont /Helvetica-Oblique"
        else:
            extra = " /BaseFont /Helvetica"
        fobj = f"{len(objects) + 1} 0 obj\n<< /Type /Font {subtype}{extra} >>\nendobj\n"
        _add(fobj.encode())

    # Build the final PDF
    xref_offset = len(b"".join(objects))
    xref: list[bytes] = [b"xref\n", f"0 {len(objects) + 1}\n".encode()]
    xref.append(b"0000000000 65535 f \n")
    for off in offsets:
        xref.append(f"{off:010d} 00000 n \n".encode())
    xref.append(b"trailer\n")
    xref.append(f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode())
    xref.append(b"startxref\n")
    xref.append(f"{xref_offset}\n".encode())
    xref.append(b"%%EOF\n")

    return b"".join(objects) + b"".join(xref)


# ---------------------------------------------------------------------------
# Facts-facing helper (mirrors write_report in report.py)
# ---------------------------------------------------------------------------


def write_report_pdf(path: str | None = None, facts: Any | None = None) -> str:
    """Build the monthly report and write it as a branded PDF; returns the path."""
    from finance_agent.report import build_report

    path = path or "reports/monthly_report.pdf"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pdf = build_report_pdf(build_report(facts))
    with open(path, "wb") as fh:
        fh.write(pdf)
    return path


if __name__ == "__main__":  # pragma: no cover
    print(write_report_pdf())
