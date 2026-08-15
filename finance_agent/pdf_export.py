"""Branded PDF export for the monthly report — hand-rolled, stdlib-only.

Zero new dependencies (matching the project's ethos: the digest is stdlib-only,
storage is stdlib ``sqlite3``, and heavy deps like xgboost were removed). This
module writes a valid, paginated A4 PDF directly — objects, xref table, and
content streams — using the PDF standard-14 base fonts (Helvetica family), so
no font files are embedded and any viewer renders it.

Scope and honest limits (see docs/KNOWN_LIMITATIONS.md §13):

* **WinAnsi text only.** Built-in fonts use ``WinAnsiEncoding`` (≈ cp1252).
  The report's ``± · —`` are WinAnsi-safe; emoji such as ``⚠️`` are mapped to
  text markers (``[over]``) and any other non-WinAnsi char becomes ``?``.
* **Simple layout engine.** A flowing block model (headings, paragraphs,
  bullets, tables, rules) with greedy word-wrap using a Helvetica width table.
  No arbitrary nesting, images, or vector charts.
* **Deterministic.** Same markdown in → identical bytes out (the report's
  generation timestamp comes from the markdown itself, not from this writer).

Layout: A4 portrait, navy brand band on every page, accent-rule section
headings, shaded table headers with alternating row tint, and a footer with
"Page X of Y" plus the generation timestamp.
"""

from __future__ import annotations

import os
import re
from typing import Any

# --------------------------------------------------------------------------- #
# Geometry (points; A4 = 595 x 842)
# --------------------------------------------------------------------------- #

PAGE_W, PAGE_H = 595, 842
MARGIN_X = 48
CONTENT_W = PAGE_W - 2 * MARGIN_X  # 499
HEADER_H = 40
TOP_Y = PAGE_H - HEADER_H - 18  # body starts just below the brand band
BOTTOM_Y = 64  # body must not flow into the footer zone
FOOTER_Y = 30

LINE_H = 13.5  # 9.5pt body text leading

# Brand palette (RGB 0..1)
NAVY = (0.086, 0.196, 0.318)
NAVY_DARK = (0.055, 0.13, 0.22)
ACCENT = (0.0, 0.45, 0.36)
INK = (0.13, 0.15, 0.18)
GRAY = (0.45, 0.48, 0.52)
LIGHT_BAND = (0.925, 0.94, 0.955)
GRID = (0.78, 0.81, 0.85)
WHITE = (1.0, 1.0, 1.0)

F_REG, F_BOLD, F_OBL = "F1", "F2", "F3"

# --------------------------------------------------------------------------- #
# Helvetica width table (units per 1000 em — standard Type1 AFM values).
# Bold runs are measured at +6% (Helvetica-Bold is wider). Unknown chars
# default to 500 so wrapping never under-estimates badly.
# --------------------------------------------------------------------------- #

_HELV: dict[str, int] = {
    " ": 278,
    "!": 278,
    '"': 355,
    "#": 556,
    "$": 556,
    "%": 889,
    "&": 667,
    "'": 191,
    "(": 333,
    ")": 333,
    "*": 389,
    "+": 584,
    ",": 278,
    "-": 333,
    ".": 278,
    "/": 278,
    "0": 556,
    "1": 556,
    "2": 556,
    "3": 556,
    "4": 556,
    "5": 556,
    "6": 556,
    "7": 556,
    "8": 556,
    "9": 556,
    ":": 278,
    ";": 278,
    "<": 584,
    "=": 584,
    ">": 584,
    "?": 556,
    "@": 1015,
    "A": 667,
    "B": 667,
    "C": 722,
    "D": 722,
    "E": 667,
    "F": 611,
    "G": 778,
    "H": 722,
    "I": 278,
    "J": 500,
    "K": 667,
    "L": 556,
    "M": 833,
    "N": 722,
    "O": 778,
    "P": 667,
    "Q": 778,
    "R": 722,
    "S": 667,
    "T": 611,
    "U": 722,
    "V": 667,
    "W": 944,
    "X": 667,
    "Y": 667,
    "Z": 611,
    "[": 278,
    "\\": 278,
    "]": 278,
    "^": 469,
    "_": 556,
    "`": 333,
    "a": 556,
    "b": 556,
    "c": 500,
    "d": 556,
    "e": 556,
    "f": 278,
    "g": 556,
    "h": 556,
    "i": 222,
    "j": 222,
    "k": 500,
    "l": 222,
    "m": 833,
    "n": 556,
    "o": 556,
    "p": 556,
    "q": 556,
    "r": 333,
    "s": 500,
    "t": 278,
    "u": 556,
    "v": 500,
    "w": 722,
    "x": 500,
    "y": 500,
    "z": 500,
    "{": 334,
    "|": 260,
    "}": 334,
    "~": 584,
    # WinAnsi extras the report actually emits
    "—": 1000,
    "·": 278,
    "±": 584,
    "–": 500,
    "€": 556,
    "’": 222,
    "“": 333,
    "”": 333,
    "•": 350,
    "✓": 500,
    "→": 500,
    "×": 584,
}


def _char_w(ch: str, size: float, bold: bool = False) -> float:
    k = 1.06 if bold else 1.0
    return _HELV.get(ch, 500) / 1000 * size * k


def _text_w(text: str, size: float, bold: bool = False) -> float:
    return sum(_char_w(c, size, bold) for c in text)


# --------------------------------------------------------------------------- #
# Text sanitization & escaping (WinAnsi)
# --------------------------------------------------------------------------- #

_EMOJI_MAP = {
    "⚠️": "[over]",
    "⚠": "[over]",
    "💸": "[$]",
    "✅": "[ok]",
    "❌": "[x]",
    "🔴": "[!]",
    "🟢": "[ok]",
    "📈": "[up]",
    "📉": "[down]",
    "🚨": "[!]",
    "🛡️": "[!]",
    "🛡": "[!]",
    "🎯": "[goal]",
    "👥": "[users]",
    "📅": "[weekly]",
    "📄": "[report]",
    "⬇️": "[download]",
    "⬇": "[download]",
    "⚡": "[fast]",
}


def sanitize_winansi(text: str) -> str:
    """Map emoji to text markers and drop anything WinAnsi can't encode.

    Built-in PDF fonts are WinAnsi (≈ cp1252); emoji and astral chars cannot
    be represented. Everything the report actually emits (± · —) is safe.
    """
    for emoji, marker in _EMOJI_MAP.items():
        text = text.replace(emoji, marker)
    # Variation selector U+FE0F often trails emoji — strip it.
    text = text.replace("\ufe0f", "")
    out: list[str] = []
    for ch in text:
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


_ESCAPES = str.maketrans({"\\": "\\\\", "(": "\\(", ")": "\\)"})


def _esc(text: str) -> str:
    return sanitize_winansi(text).translate(_ESCAPES)


# --------------------------------------------------------------------------- #
# Markdown → layout blocks (the subset build_report() emits)
# --------------------------------------------------------------------------- #

_PARA_RE = re.compile(r"\*\*(.+?)\*\*")  # inline **bold** runs


def _runs(text: str) -> list[tuple[str, str]]:
    """Split a line into (style, text) runs; only inline **bold** is parsed.

    ``_italic_`` lines are handled at block level (whole-line italics), so no
    word-level underscore parsing is attempted here.
    """
    parts = _PARA_RE.split(text)
    out: list[tuple[str, str]] = []
    for i, part in enumerate(parts):
        if not part:
            continue
        out.append((F_BOLD if i % 2 == 1 else F_REG, part))
    return out or [(F_REG, "")]


def _parse_blocks(markdown: str) -> list[tuple[str, Any]]:
    """Turn the report markdown into layout blocks.

    Block kinds: ("title", text) ("subtitle", text) ("h2", text)
    ("para", text) ("bullet", text) ("table", rows) ("rule", None)
    ("italic", text). Table separator rows (|---|---|) are dropped.
    """
    blocks: list[tuple[str, Any]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue
        if line.startswith("# "):
            blocks.append(("title", line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:].strip()))
        elif line.startswith("- "):
            blocks.append(("bullet", line[2:].strip()))
        elif line.strip() == "---":
            blocks.append(("rule", None))
        elif len(line) > 2 and line.startswith("_") and line.endswith("_"):
            blocks.append(("italic", line.strip("_")))
        else:
            blocks.append(("para", line.strip()))
        i += 1
    return blocks


# --------------------------------------------------------------------------- #
# Layout engine — flows blocks onto pages, then emits the PDF
# --------------------------------------------------------------------------- #


class _Layout:
    """Accumulates per-page content-stream commands (bytes) with a y cursor."""

    def __init__(self, generation: str) -> None:
        self.generation = generation
        self.pages: list[list[bytes]] = []
        self._cmds: list[bytes] = []
        self.y: float = TOP_Y
        self.new_page()

    # -- page & geometry ---------------------------------------------------- #

    def new_page(self) -> None:
        if self._cmds:
            self.pages.append(self._cmds)
        self._cmds = [self._header_bytes()]
        self.y = TOP_Y

    def ensure(self, height: float) -> None:
        """Start a new page if `height` no longer fits below the cursor."""
        if self.y - height < BOTTOM_Y:
            self.new_page()

    # -- primitives --------------------------------------------------------- #

    def _emit(self, prefix: bytes, text: str, suffix: bytes) -> None:
        self._cmds.append(prefix + _esc(text).encode("cp1252", "replace") + suffix)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float = 9.5,
        font: str = F_REG,
        color: tuple[float, float, float] = INK,
    ) -> None:
        r, g, b = color
        self._emit(
            (
                f"BT /{font} {size} Tf {r:.3f} {g:.3f} {b:.3f} rg 1 0 0 1 {x:.2f} {y:.2f} Tm ("
            ).encode("ascii"),
            text,
            b") Tj ET\n",
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: tuple[float, float, float] | None = None,
        stroke: tuple[float, float, float] | None = None,
    ) -> None:
        cmds = [f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re ".encode("ascii")]
        if fill is not None:
            cmds.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} f\n".encode("ascii"))
        if stroke is not None:
            cmds.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} S\n".encode("ascii"))
        self._cmds.extend(cmds)

    def rule(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: tuple[float, float, float],
        width: float = 0.8,
    ) -> None:
        """Draw a straight line from (x1, y1) to (x2, y2)."""
        self._cmds.append(
            (
                f"{width:.2f} w {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG "
                f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S\n"
            ).encode("ascii")
        )

    # -- blocks ------------------------------------------------------------- #

    def _wrap(
        self,
        runs: list[tuple[str, str]],
        max_w: float,
        size: float,
    ) -> list[list[tuple[str, str]]]:
        """Greedy word-wrap `runs` into lines that fit `max_w`."""
        words: list[tuple[str, str, float]] = []  # (style, word, width)
        for style, chunk in runs:
            for word in chunk.split(" "):
                if word:
                    words.append((style, word, _text_w(word, size, bold=style == F_BOLD)))
        lines: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        cur_w = 0.0
        for style, word, w in words:
            sep = _char_w(" ", size) if cur else 0.0
            if cur and cur_w + sep + w > max_w:
                lines.append(cur)
                cur, cur_w = [], 0.0
                sep = 0.0
            if cur:
                cur_w += sep
            cur.append((style, word))
            cur_w += w
        if cur:
            lines.append(cur)
        return lines or [[(F_REG, "")]]

    def draw_runs(
        self,
        x: float,
        y: float,
        runs: list[tuple[str, str]],
        size: float,
        color: tuple[float, float, float],
    ) -> None:
        cx = x
        for style, chunk in runs:
            self.text(cx, y, chunk, size=size, font=style, color=color)
            cx += _text_w(chunk, size, bold=style == F_BOLD)

    def para(
        self,
        text: str,
        size: float = 9.5,
        color: tuple[float, float, float] = INK,
        indent: float = 0.0,
        font: str = F_REG,
    ) -> None:
        runs = _runs(text)
        if font == F_OBL:
            runs = [(F_OBL, t) for _, t in runs]
        max_w = CONTENT_W - indent
        lines = self._wrap(runs, max_w, size)
        need = len(lines) * LINE_H
        self.ensure(need)
        for line in lines:
            self.draw_runs(MARGIN_X + indent, self.y, line, size, color)
            self.y -= LINE_H

    def bullet(self, text: str) -> None:
        runs = [(F_REG, "• ")] + _runs(text)
        max_w = CONTENT_W - 16
        lines = self._wrap(runs, max_w, 9.5)
        need = len(lines) * LINE_H
        self.ensure(need)
        for line in lines:
            self.draw_runs(MARGIN_X + 16, self.y, line, 9.5, INK)
            self.y -= LINE_H

    def heading(self, text: str) -> None:
        self.ensure(30)
        self.y -= 6
        self.text(MARGIN_X, self.y, text, size=13, font=F_BOLD, color=NAVY)
        self.rule(MARGIN_X, self.y - 4, MARGIN_X + 46, self.y - 4, ACCENT, width=2.4)
        self.y -= 24

    def title(self, text: str) -> None:
        self.text(MARGIN_X, self.y, text, size=19, font=F_BOLD, color=NAVY_DARK)
        self.rule(MARGIN_X, self.y - 5, PAGE_W - MARGIN_X, self.y - 5, NAVY, width=1.2)
        self.y -= 22

    def table(self, rows: list[list[str]]) -> None:
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        size = 8.5
        pad = 4.0
        # Column widths proportional to content, clamped, then scaled to fit.
        raw = [
            max(_text_w(cell, size) for cell in col) + 2 * pad for col in zip(*rows, strict=True)
        ]
        total = sum(raw)
        scale = min(1.0, CONTENT_W / total) if total else 1.0
        widths = [max(28.0, w * scale) for w in raw]
        # Redistribute rounding slack into the last column.
        slack = CONTENT_W - sum(widths)
        widths[-1] += slack
        col_x = [MARGIN_X + sum(widths[:i]) for i in range(ncols)]

        header = rows[0]
        header_lines = max(
            len(self._wrap([(F_BOLD, c)], widths[i], size)) for i, c in enumerate(header)
        )
        header_h = header_lines * (LINE_H - 1) + 7

        for ridx, row in enumerate(rows):
            is_header = ridx == 0
            cells_lines = [
                self._wrap([(F_BOLD, c) if is_header else (F_REG, c)], widths[i], size)
                for i, c in enumerate(row)
            ]
            lines_n = max(len(cl) for cl in cells_lines)
            h = header_h if is_header else lines_n * (LINE_H - 1) + 6
            self.ensure(h)
            if is_header:
                self.rect(MARGIN_X, self.y - h, CONTENT_W, h, fill=NAVY)
            elif ridx % 2 == 0:
                self.rect(MARGIN_X, self.y - h, CONTENT_W, h, fill=LIGHT_BAND)
            for i, cl in enumerate(cells_lines):
                color = WHITE if is_header else INK
                ty = self.y - h / 2 + (len(cl) - 1) * (LINE_H - 1) / 2
                for line in cl:
                    self.draw_runs(col_x[i] + pad, ty + (LINE_H - 1) * 0.85, line, size, color)
                    ty -= LINE_H - 1
            # grid
            top, bottom = self.y, self.y - h
            for i in range(ncols + 1):
                gx = MARGIN_X + sum(widths[:i])
                self.rule(gx, top, gx, bottom, GRID, width=0.4)
            self.rule(MARGIN_X, top, MARGIN_X + CONTENT_W, top, GRID, width=0.4)
            self.rule(MARGIN_X, bottom, MARGIN_X + CONTENT_W, bottom, GRID, width=0.4)
            self.y -= h
        self.y -= 4

    def hrule(self) -> None:
        self.ensure(12)
        self.rule(MARGIN_X, self.y - 4, PAGE_W - MARGIN_X, self.y - 4, GRID, width=0.6)
        self.y -= 12

    def italic(self, text: str) -> None:
        self.para(text, size=8.5, color=GRAY, font=F_OBL)

    # -- chrome ------------------------------------------------------------- #

    def _header_bytes(self) -> bytes:
        cmds: list[bytes] = []
        # navy band
        cmds.append(
            f"0.086 0.196 0.318 rg 0 {PAGE_H - HEADER_H} {PAGE_W} {HEADER_H} re f\n".encode("ascii")
        )
        cmds.append(f"0.055 0.13 0.22 rg 0 {PAGE_H - HEADER_H} {PAGE_W} 3 re f\n".encode("ascii"))
        cmds.append(
            self._text_bytes(
                MARGIN_X, PAGE_H - 26, "FinSight Agent", size=12, font=F_BOLD, color=WHITE
            )
        )
        cmds.append(
            self._text_bytes(
                MARGIN_X, PAGE_H - 13, "Personal Finance Report", size=8.5, color=(0.8, 0.85, 0.9)
            )
        )
        right = f"Generated {self.generation}"
        cmds.append(
            self._text_bytes(
                PAGE_W - MARGIN_X - _text_w(right, 8.5),
                PAGE_H - 26,
                right,
                size=8.5,
                font=F_REG,
                color=(0.8, 0.85, 0.9),
            )
        )
        return b"".join(cmds)

    def _text_bytes(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        font: str = F_REG,
        color: tuple[float, float, float] = GRAY,
    ) -> bytes:
        r, g, b = color
        return (
            f"BT /{font} {size} Tf {r:.3f} {g:.3f} {b:.3f} rg 1 0 0 1 {x:.2f} {y:.2f} Tm (".encode(
                "ascii"
            )
            + _esc(text).encode("cp1252", "replace")
            + b") Tj ET\n"
        )

    def finish(self, total: int) -> None:
        """Append the page footer (needs the final page count) to each page."""
        self.pages.append(self._cmds)
        for idx, page in enumerate(self.pages, start=1):
            footer = [
                f"0.78 0.81 0.85 RG 48 {FOOTER_Y + 12:.2f} m {PAGE_W - 48:.2f} {FOOTER_Y + 12:.2f} l S\n".encode(
                    "ascii"
                ),
            ]
            left = "finsight-agent"
            page_no = f"Page {idx} of {total}"
            footer.append(
                self._text_bytes(
                    MARGIN_X,
                    FOOTER_Y,
                    left,
                    size=8,
                    font=F_REG,
                    color=GRAY,
                )
            )
            footer.append(
                self._text_bytes(
                    (PAGE_W - _text_w(page_no, 8)) / 2,
                    FOOTER_Y,
                    page_no,
                    size=8,
                    font=F_REG,
                    color=GRAY,
                )
            )
            footer.append(
                self._text_bytes(
                    PAGE_W - MARGIN_X - _text_w(self.generation, 8),
                    FOOTER_Y,
                    self.generation,
                    size=8,
                    font=F_REG,
                    color=GRAY,
                )
            )
            page.extend(footer)


# --------------------------------------------------------------------------- #
# PDF emission (objects, xref, trailer)
# --------------------------------------------------------------------------- #


def _content_stream_bytes(cmds: list[bytes]) -> bytes:
    return b"".join(cmds)


def build_report_pdf(markdown: str) -> bytes:
    """Render `markdown` (the report body) to a complete, valid PDF (bytes)."""
    blocks = _parse_blocks(markdown)
    if not blocks:
        blocks = [("para", "No report content.")]

    # Pull the generation timestamp out of the report's subtitle line so the
    # footer and header agree with what the report claims.
    generation = ""
    for kind, payload in blocks:
        if kind == "subtitle":
            m = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", payload)
            if m:
                generation = m.group(0)
    if not generation:
        generation = "FinSight Agent"

    layout = _Layout(generation)
    for kind, payload in blocks:
        if kind == "title":
            layout.title(payload)
        elif kind == "subtitle":
            layout.italic(payload)
        elif kind == "h2":
            layout.heading(payload)
        elif kind == "para":
            layout.para(payload)
        elif kind == "bullet":
            layout.bullet(payload)
        elif kind == "table":
            layout.table(payload)
        elif kind == "rule":
            layout.hrule()
        elif kind == "italic":
            layout.italic(payload)
    layout.finish(len(layout.pages))

    return _assemble(layout.pages)


def _assemble(pages: list[list[bytes]]) -> bytes:
    """Serialize page command lists into a PDF file with a correct xref table."""
    n = len(pages)
    font_base = 3 + n  # object ids: 1 catalog, 2 pages, 3..3+n-1 pages
    f_reg_id = font_base
    f_bold_id = font_base + 1
    f_obl_id = font_base + 2
    content_base = font_base + 3
    total_objects = content_base + n - 1  # last object id

    body = bytearray()
    body += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"

    def obj(num: int, data: bytes) -> None:
        nonlocal body
        body += f"{num} 0 obj\n".encode("ascii")
        body += data
        body += b"endobj\n"

    offsets: dict[int, int] = {0: 0}
    header = {num: len(body) for num in range(1, total_objects + 1)}
    offsets.update(header)

    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>\n")
    kids = " ".join(f"{i} 0 R" for i in range(3, 3 + n))
    obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {n} >>\n".encode("ascii"))

    for i in range(n):
        page_num = 3 + i
        content_num = content_base + i
        obj(
            page_num,
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                f"/Resources << /Font << /{F_REG} {f_reg_id} 0 R /{F_BOLD} {f_bold_id} 0 R "
                f"/{F_OBL} {f_obl_id} 0 R >> >> /Contents {content_num} 0 R >>\n"
            ).encode("ascii"),
        )

    def font_obj(base: str) -> bytes:
        return f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>\n".encode(
            "ascii"
        )

    obj(f_reg_id, font_obj("Helvetica"))
    obj(f_bold_id, font_obj("Helvetica-Bold"))
    obj(f_obl_id, font_obj("Helvetica-Oblique"))

    for i in range(n):
        stream = _content_stream_bytes(pages[i])
        data = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream\n"
        obj(content_base + i, data)

    xref_pos = len(body)
    body += f"xref\n0 {total_objects + 1}\n".encode("ascii")
    body += b"0000000000 65535 f \n"
    for num in range(1, total_objects + 1):
        body += f"{offsets[num]:010d} 00000 n \n".encode("ascii")
    body += (
        f"trailer\n<< /Size {total_objects + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(body)


# --------------------------------------------------------------------------- #
# Facts-facing helper (mirrors write_report in report.py)
# --------------------------------------------------------------------------- #


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
