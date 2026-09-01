"""PDF layout engine — flows blocks onto pages, then emits the PDF.

Extracted from ``pdf_export.py``.  The ``_Layout`` class accumulates
per-page content-stream commands with a y cursor and handles word-wrapping,
tables, headings, and page chrome (header band + footer).
"""

from __future__ import annotations

from finance_agent.pdf_text import (
    ACCENT,
    BOTTOM_Y,
    CONTENT_W,
    F_BOLD,
    F_OBL,
    F_REG,
    FOOTER_Y,
    GRAY,
    GRID,
    INK,
    LIGHT_BAND,
    LINE_H,
    MARGIN_X,
    NAVY,
    NAVY_DARK,
    PAGE_H,
    PAGE_W,
    TOP_Y,
    WHITE,
    _esc,
    _runs,
    _text_w,
)


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
        """Start a new page if ``height`` no longer fits below the cursor."""
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
    ) -> None -> None:
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
    ) -> None -> None:
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
    ) -> None -> None:
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
        """Greedy word-wrap ``runs`` into lines that fit ``max_w``."""
        words: list[tuple[str, str, float]] = []  # (style, word, width)
        for style, chunk in runs:
            for word in chunk.split(" "):
                if word:
                    words.append((style, word, _text_w(word, size, bold=style == F_BOLD)))
        lines: list[list[tuple[str, str]]] = []
        cur: list[tuple[str, str]] = []
        cur_w = 0.0
        for style, word, w in words:
            sep = _text_w(" ", size) if cur else 0.0
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
    ) -> None -> None:
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
    ) -> None -> None:
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
            f"0.086 0.196 0.318 rg 0 {PAGE_H - 40} {PAGE_W} 40 re f\n".encode("ascii")
        )
        cmds.append(f"0.055 0.13 0.22 rg 0 {PAGE_H - 40} {PAGE_W} 3 re f\n".encode("ascii"))
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
