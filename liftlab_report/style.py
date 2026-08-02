"""Workbook formatting: fonts, number formats, widths, panes, print setup.

One place decides what a seconds cell looks like, so a number never lands in a
cell as a bare float. The colour convention is declared once here and applied
mechanically by verdict_fill(); it is documented for the reader in the legend
on READ THIS FIRST, generated from the same constants.
"""

from __future__ import annotations

from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT_NAME = "Arial"

# ── fonts ────────────────────────────────────────────────────────────────────
H1 = Font(name=FONT_NAME, bold=True, size=14)
H2 = Font(name=FONT_NAME, bold=True, size=11)
BODY = Font(name=FONT_NAME, size=10)
BODY_BOLD = Font(name=FONT_NAME, bold=True, size=10)
BODY_ITALIC = Font(name=FONT_NAME, size=10, italic=True)
CAPTION = Font(name=FONT_NAME, size=9, italic=True, color="595959")
WARN_FONT = Font(name=FONT_NAME, bold=True, size=10, color="9C5700")
ERA_FONT = Font(name=FONT_NAME, bold=True, size=10, color="9C0006")

# ── number formats ───────────────────────────────────────────────────────────
# Seconds carry their unit in the format so a bare 2.31 is never ambiguous.
F_SEC = '0.00"s"'
F_SEC_PLAIN = "0.00"
F_INT = "#,##0"
F_PCT = "0.0%"            # stored as a FRACTION; the format supplies the %
F_TS = "yyyy-mm-dd hh:mm"
F_RATE = '0.00'
F_TEXT = "@"

# ── colour convention (mirrored in the READ THIS FIRST legend) ───────────────
C_GOOD = "C6EFCE"         # green  — clears the threshold favourably
C_BAD = "FFC7CE"          # red    — exceeds the compliance cliff
C_WARN = "FFEB9C"         # amber  — inconclusive, keep collecting
C_NA = "D9D9D9"           # grey   — not measurable
C_HEADER = "1F3864"
C_HEADER_TEXT = "FFFFFF"
C_ERA = "FCE4EC"

FILL_GOOD = PatternFill("solid", start_color=C_GOOD, end_color=C_GOOD)
FILL_BAD = PatternFill("solid", start_color=C_BAD, end_color=C_BAD)
FILL_WARN = PatternFill("solid", start_color=C_WARN, end_color=C_WARN)
FILL_NA = PatternFill("solid", start_color=C_NA, end_color=C_NA)
FILL_ERA = PatternFill("solid", start_color=C_ERA, end_color=C_ERA)
FILL_HEADER = PatternFill("solid", start_color=C_HEADER, end_color=C_HEADER)

HEADER_FONT = Font(name=FONT_NAME, bold=True, size=10, color=C_HEADER_TEXT)

WRAP = Alignment(wrap_text=True, vertical="top")
WRAP_CENTRE = Alignment(wrap_text=True, vertical="center")
TOP = Alignment(vertical="top")

THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

MAX_COL_WIDTH = 60
MIN_COL_WIDTH = 8


def verdict_fill(verdict: str | None, suppressed: bool = False):
    """Map a mechanical verdict string onto the declared colour convention."""
    if suppressed:
        return FILL_NA
    v = (verdict or "").lower()
    if not v:
        return None
    if "not measurable" in v or "not comparable" in v:
        return FILL_NA
    if "straddles" in v or "keep collecting" in v or "not computable" in v:
        return FILL_WARN
    if "clears threshold (below)" in v:
        return FILL_GOOD
    if "clears threshold (above)" in v:
        return FILL_BAD
    return None


def confidence_fill(level: str):
    return {"HIGH": FILL_GOOD, "MEDIUM": FILL_WARN}.get(
        level, FILL_NA)


def cell(ws, row, col, value, fmt=None, font=None, fill=None, wrap=False,
         border=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font = font or BODY
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = fill
    if wrap:
        c.alignment = WRAP
    else:
        c.alignment = TOP
    if border:
        c.border = BOX
    return c


def header_row(ws, row, headers, widths=None):
    """A styled header row. Returns the next free row."""
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = HEADER_FONT
        c.fill = FILL_HEADER
        c.alignment = WRAP_CENTRE
        c.border = BOX
    ws.row_dimensions[row].height = 30
    if widths:
        set_widths(ws, widths)
    return row + 1


def title(ws, text, row=1):
    c = ws.cell(row=row, column=1, value=text)
    c.font = H1
    return row + 1


def section(ws, row, text):
    c = ws.cell(row=row, column=1, value=text)
    c.font = H2
    return row + 1


def caption(ws, row, text, col=1):
    """The one-line plain-English take-away that sits directly above a chart."""
    c = ws.cell(row=row, column=col, value=text)
    c.font = CAPTION
    c.alignment = WRAP
    return row + 1


def banner(ws, row, text, fill=None, font=None, height=32, width_cols=12):
    c = ws.cell(row=row, column=1, value=text)
    c.font = font or WARN_FONT
    c.fill = fill or FILL_WARN
    c.alignment = WRAP
    ws.row_dimensions[row].height = height
    try:
        ws.merge_cells(start_row=row, start_column=1,
                       end_row=row, end_column=width_cols)
    except ValueError:
        pass
    return row + 1


def set_widths(ws, widths: dict):
    """{column_index: width} — capped, and never narrower than MIN_COL_WIDTH."""
    for col, w in widths.items():
        letter = get_column_letter(col)
        want = max(MIN_COL_WIDTH, min(MAX_COL_WIDTH, w))
        cur = ws.column_dimensions[letter].width or 0
        if want > cur:
            ws.column_dimensions[letter].width = want


def autofit(ws, max_row=None, wrap_cols=(), cap=MAX_COL_WIDTH):
    """Size every column to its widest cell, capped. Columns named in
    wrap_cols get wrap_text instead of unbounded width."""
    widths: dict[int, int] = {}
    limit = max_row or ws.max_row
    for row in ws.iter_rows(min_row=1, max_row=min(limit, ws.max_row)):
        for c in row:
            if c.value is None:
                continue
            longest = max((len(part) for part in str(c.value).split("\n")),
                          default=0)
            widths[c.column] = max(widths.get(c.column, 0), longest)
    for col, w in widths.items():
        letter = get_column_letter(col)
        if col in wrap_cols:
            ws.column_dimensions[letter].width = min(cap, max(MIN_COL_WIDTH, 45))
        else:
            ws.column_dimensions[letter].width = min(cap, max(MIN_COL_WIDTH, w + 2))


def wrap_column(ws, col: int, first_row: int, last_row: int, width: int = 45):
    ws.column_dimensions[get_column_letter(col)].width = width
    for r in range(first_row, last_row + 1):
        ws.cell(row=r, column=col).alignment = WRAP


def freeze_below(ws, row: int, col: int = 1):
    """Freeze panes so the header row stays visible while scrolling."""
    ws.freeze_panes = f"{get_column_letter(col)}{row + 1}"


def autofilter(ws, header_row_idx: int, n_cols: int, last_row: int):
    if last_row <= header_row_idx:
        return
    ws.auto_filter.ref = (f"A{header_row_idx}:"
                          f"{get_column_letter(n_cols)}{last_row}")


def verdict_conditional_formatting(ws, col: int, first_row: int, last_row: int):
    """Live conditional-formatting rules on a verdict column, keyed on the
    verdict text, so the colour convention survives the reader sorting,
    filtering or editing the sheet — static fills alone would not.

    Rules are stopIfTrue and ordered most-specific-first: 'not measurable'
    wins over everything, then inconclusive, then the two clear verdicts."""
    if last_row < first_row:
        return
    letter = get_column_letter(col)
    ref = f"{letter}{first_row}:{letter}{last_row}"
    first = f"{letter}{first_row}"
    rules = [
        ("not measurable", FILL_NA),
        ("not comparable", FILL_NA),
        ("straddles", FILL_WARN),
        ("keep collecting", FILL_WARN),
        ("clears threshold (below)", FILL_GOOD),
        ("clears threshold (above)", FILL_BAD),
    ]
    for needle, fill in rules:
        ws.conditional_formatting.add(ref, FormulaRule(
            formula=[f'ISNUMBER(SEARCH("{needle}",{first}))'],
            fill=fill, stopIfTrue=True))


def print_setup(ws, repeat_row: int | None = None, landscape=True,
                fit_width=True):
    """Landscape, fit-to-width, repeating header rows — so it prints sanely."""
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    if fit_width:
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = False
    if repeat_row:
        ws.print_title_rows = f"{repeat_row}:{repeat_row}"
