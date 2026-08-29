"""PDF text measurement, sanitization, and escaping utilities.

Extracted from ``pdf_export.py`` to keep the font width table and WinAnsi
mapping in a focused module.  The layout engine and PDF assembly remain in
``pdf_export.py``.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Geometry (points; A4 = 595 x 842)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Helvetica width table (units per 1000 em — standard Type1 AFM values).
# Bold runs are measured at +6% (Helvetica-Bold is wider).  Unknown chars
# default to 500 so wrapping never under-estimates badly.
# ---------------------------------------------------------------------------

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
    "\u2019": 222,
    "\u201c": 333,
    "\u201d": 333,
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


# ---------------------------------------------------------------------------
# Text sanitization & escaping (WinAnsi)
# ---------------------------------------------------------------------------

# CP1252 (WinAnsi) — the superset of Latin-1 that PDF built-in fonts use.
_WINANSI_SAFE = set(range(0x20, 0x7F)) | set(range(0xA0, 0x100))
# Plus the few extra CP1252 chars that share code-points with Unicode but
# aren't in Latin-1 proper.
_WINANSI_EXTRA = {0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2022, 0x20AC}

# Map non-WinAnsi chars to a safe text marker (used in footnotes / audit).
_WINANSI_MAP: dict[int, str] = {
    0x2030: "[per-mille]",  # ‰
    0x2610: "[box]",        # ☐
    0x2713: "V",            # ✓  (keep the glyph where the font has it)
    0x2717: "X",            # ✗
}

# Sentinel for chars that are mapped via _WINANSI_MAP.
_MAPPED = frozenset(_WINANSI_MAP.keys())


def sanitize_winansi(text: str) -> str:
    """Replace non-WinAnsi characters with safe ASCII approximations.

    Characters in ``_WINANSI_MAP`` get their text marker; anything else
    outside the WinAnsi range becomes ``?`` so the PDF stays valid.
    """
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp in _WINANSI_SAFE or cp in _WINANSI_EXTRA:
            out.append(ch)
        elif cp in _MAPPED:
            out.append(_WINANSI_MAP[cp])
        else:
            out.append("?")  # pragma: no cover – non-Latin input
    return "".join(out)


_ESCAPES = str.maketrans({"\\": "\\\\", "(": "\\(", ")": "\\)"})


def _esc(text: str) -> str:
    return sanitize_winansi(text).translate(_ESCAPES)


# ---------------------------------------------------------------------------
# Markdown → layout blocks (the subset build_report() emits)
# ---------------------------------------------------------------------------

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


def _parse_blocks(markdown: str) -> list[tuple[str, object]]:
    """Turn the report markdown into layout blocks.

    Block kinds: ("title", text) ("subtitle", text) ("h2", text)
    ("para", text) ("bullet", text) ("table", rows) ("rule", None)
    ("italic", text).  Table separator rows (|---|---|) are dropped.
    """
    blocks: list[tuple[str, object]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()

        # Heading 1 (title)
        if ln.startswith("# ") and not ln.startswith("## "):
            blocks.append(("title", ln[2:].strip()))
            i += 1
            continue

        # Heading 2
        if ln.startswith("## "):
            blocks.append(("h2", ln[3:].strip()))
            i += 1
            continue

        # Horizontal rule
        if ln.startswith("---") and set(ln.replace("-", "")) <= {" ", "="}:
            blocks.append(("rule", None))
            i += 1
            continue

        # Table (collect consecutive pipe-delimited rows)
        if "|" in ln and ln.strip().startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i]:
                row_ln = lines[i].strip()
                # skip separator rows (|---|---|)
                if re.match(r"^\|[\s\-:|]+\|$", row_ln):
                    i += 1
                    continue
                cells = [c.strip() for c in row_ln.split("|")]
                # split('|') gives '' at both ends
                rows.append([c for c in cells if c != ""])
                i += 1
            if rows:
                blocks.append(("table", rows))
            continue

        # Bullet
        if ln.startswith("- ") or ln.startswith("* "):
            blocks.append(("bullet", ln[2:].strip()))
            i += 1
            continue

        # Italic-only paragraph (whole line in _italic_ markers)
        stripped = ln.strip()
        if stripped.startswith("_") and stripped.endswith("_") and len(stripped) > 2:
            blocks.append(("italic", stripped[1:-1]))
            i += 1
            continue

        # Subtitle line — heuristic: short, non-empty, after a title
        if stripped and not stripped.startswith("#") and not stripped.startswith("|"):
            # If previous block was a title, treat as subtitle
            if blocks and blocks[-1][0] == "title":
                blocks.append(("subtitle", stripped))
                i += 1
                continue

        # Regular paragraph (may contain multiple consecutive non-blank lines)
        para_lines: list[str] = []
        while i < len(lines):
            pln = lines[i].rstrip()
            if not pln.strip():
                i += 1
                break
            # Don't consume table/header/bullet/rule starts
            if pln.startswith("#") or pln.startswith("|") or pln.startswith("---") or pln.startswith("- ") or pln.startswith("* "):
                break
            para_lines.append(pln)
            i += 1
        if para_lines:
            blocks.append(("para", " ".join(para_lines)))

    return blocks
