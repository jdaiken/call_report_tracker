"""The five-page PDF (US Letter), drawn from the same Report as the email.

Every page is a fixed layout with an explicit page break, so the document is
always exactly five pages.
"""
from __future__ import annotations

import io
import os
from xml.sax.saxutils import escape

import matplotlib
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .extras import deposits_note
from .monitor import NAME, Panel, Report
from .newsletter import FDIC_FAILURES_URL, TONE_COLOR, TONE_WORD

_FONT_DIR = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(_FONT_DIR, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")

INK, MUTED, RULE = colors.HexColor("#14181f"), colors.HexColor("#5b626c"), colors.HexColor("#dde0e4")
LINK = "#1d5fbf"
PAGE_W, PAGE_H = letter
MARGIN = 0.55 * inch
WIDTH = PAGE_W - 2 * MARGIN
GAP = 0.18 * inch
CELL_W = (WIDTH - GAP) / 2


def _st(name, size, color=INK, bold=False, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName="DejaVu-Bold" if bold else "DejaVu", fontSize=size,
                          leading=size * 1.3, textColor=color, **kw)


S = {
    "page": _st("page", 15, bold=True),
    "pno": _st("pno", 8, MUTED, alignment=TA_RIGHT),
    "label": _st("label", 7.5, MUTED, bold=True),
    "ptitle": _st("ptitle", 9.5, bold=True),
    "big": _st("big", 17, bold=True),
    "sig": _st("sig", 8, MUTED),
    "cell": _st("cell", 8.2),
    "cellm": _st("cellm", 7.5, MUTED),
    "num": _st("num", 8.5, alignment=TA_RIGHT),
    "numb": _st("numb", 8.5, bold=True, alignment=TA_RIGHT),
    "badge": _st("badge", 6.5, colors.white, bold=True),
    "tile_n": _st("tile_n", 16, bold=True),
    "body": _st("body", 9),
    "small": _st("small", 7.5, MUTED),
}


def _p(text: str, style: str) -> Paragraph:
    return Paragraph(text, S[style])


def _link(text: str, url: str | None, color: str = LINK, underline: bool = True) -> str:
    """Clickable text, blue so it is obvious it opens the source."""
    if not url:
        return escape(text)
    body = f"<u>{escape(text)}</u>" if underline else escape(text)
    return f'<link href="{escape(url)}" color="{color}">{body}</link>'



def _badge(tone: str) -> Table:
    t = Table([[_p(TONE_WORD[tone].upper(), "badge")]], colWidths=[0.58 * inch])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(TONE_COLOR[tone])),
                           ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                           ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                           ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _header(title: str, n: int) -> list:
    t = Table([[_p(escape(title), "page"), _p(f"{n} / 5", "pno")]], colWidths=[WIDTH * 0.8, WIDTH * 0.2])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.5, INK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return [t, Spacer(1, 10)]


def _img(png: bytes, width: float, scale: float = 1.0) -> Image:
    # scale < 1 keeps the full width but trims height (the chart is redrawn squatter, not cropped)
    img = Image(io.BytesIO(png))
    img.drawWidth, img.drawHeight = width, width * img.imageHeight / img.imageWidth * scale
    return img


def _panel(p: Panel, width: float = CELL_W, scale: float = 1.0) -> Table:
    inner = width - 14
    head = Table([[_p(escape(p.title), "ptitle"), _badge(p.tone)]], colWidths=[inner - 0.62 * inch, 0.62 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    content = [head, Spacer(1, 3), _p(_link(p.reading, p.url, underline=False), "big"), _p(escape(p.signal), "sig"), Spacer(1, 4)]
    content.append(_img(p.png, inner, scale) if p.png else _p("Chart unavailable", "small"))
    if p.sources:
        content += [Spacer(1, 2), _p("Source: " + " · ".join(_link(lbl, u) for lbl, u in p.sources), "small")]
    t = Table([[content]], colWidths=[width])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                           ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _grid(panels: list[Panel], scale: float = 1.0) -> Table:
    rows = [[_panel(panels[i], scale=scale), "", _panel(panels[i + 1], scale=scale) if i + 1 < len(panels) else ""] for i in range(0, len(panels), 2)]
    t = Table(rows, colWidths=[CELL_W, GAP, CELL_W])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), GAP)]))
    return t


def _summary(rep: Report) -> list:
    story = _header("Summary", 1)
    tiles = []
    for label, tone in (("RISK", "risk"), ("WATCH", "watch"), ("NORMAL", "ok")):
        tiles.append(Table([[_p(str(rep.counts[tone]), "tile_n")], [_p(label, "label")]], colWidths=[(WIDTH - 2 * GAP) / 3]))
        tiles[-1].setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE),
                                       ("LINEABOVE", (0, 0), (-1, 0), 3.5, colors.HexColor(TONE_COLOR[tone])),
                                       ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (0, 0), 6),
                                       ("BOTTOMPADDING", (0, 0), (0, 0), 0), ("TOPPADDING", (0, -1), (-1, -1), 0),
                                       ("BOTTOMPADDING", (0, -1), (-1, -1), 6)]))
    row = Table([[tiles[0], "", tiles[1], "", tiles[2]]], colWidths=[(WIDTH - 2 * GAP) / 3, GAP] * 2 + [(WIDTH - 2 * GAP) / 3])
    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [row, Spacer(1, 9)]
    if rep.summary:
        inner = [_p("WHAT'S GOING ON", "label"), Spacer(1, 2), Paragraph(escape(rep.summary), ParagraphStyle("summ", parent=S["body"], fontSize=8.7, leading=11.6))]
        box = Table([[inner]], colWidths=[WIDTH])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f6f8")),
                                 ("LINEBEFORE", (0, 0), (0, -1), 2.5, INK),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
        story += [box, Spacer(1, 8)]
    # With the plain-English summary on the page, the separate signal list is left to the email (space).
    if not rep.summary:
        story += [_p("KEY SIGNALS", "label"), Spacer(1, 2)]
    if rep.signals and not rep.summary:
        for t, x in rep.signals[:5]:
            story.append(Paragraph(f'<font color="{TONE_COLOR[t]}">●</font>  {escape(x)}', _st(f"s{t}", 8.5, leftIndent=2, spaceAfter=1)))
    elif not rep.summary:
        story.append(_p("No measures in a Watch or Risk range.", "body"))
    if not rep.summary:
        story.append(Spacer(1, 8))

    hdr = [_p("MEASURE", "label"), _p("LATEST", "label"), _p("LAST", "label"), _p("1 MO", "label"), _p("1 YR", "label"),
           _p("STATUS", "label")]
    for c in hdr[1:]:
        c.style = ParagraphStyle("lr", parent=S["label"], alignment=TA_RIGHT)
    rows, styles = [hdr], [("LINEBELOW", (0, 0), (-1, 0), 1, INK), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                           ("TOPPADDING", (0, 0), (-1, -1), 1.4), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4),
                           ("ALIGN", (5, 1), (5, -1), "RIGHT")]
    for title, grp in rep.groups:
        rows.append([_p(escape(title.upper()), "label"), "", "", "", "", ""])
        styles += [("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1)), ("TOPPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 6)]
        for r in grp:
            rows.append([_p(f"<b>{escape(r.name)}</b><br/><font size='6.5' color='#5b626c'>{escape(r.signal)}</font>", "cell"),
                         _p(_link(r.reading, r.url), "numb"), _p(escape(r.last), "num"), _p(escape(r.month), "num"),
                         _p(escape(r.year), "num"), _badge(r.tone)])
            styles.append(("LINEBELOW", (0, len(rows) - 1), (-1, len(rows) - 1), 0.4, RULE))
    table = Table(rows, colWidths=[WIDTH - 4.55 * inch, 1.15 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 1.0 * inch])
    table.setStyle(TableStyle(styles))
    story.append(table)
    return story


def _rates_page(rep: Report) -> list:
    story = _header("Rates", 2)
    hdr = [_p(x, "label") for x in ("RATE", "LATEST", "1 DAY", "1 WK", "1 MO", "1 YR", "AS OF")]
    for c in hdr[1:]:
        c.style = ParagraphStyle("lr", parent=S["label"], alignment=TA_RIGHT)
    rows, styles = [hdr], [("LINEBELOW", (0, 0), (-1, 0), 1, INK), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                           ("TOPPADDING", (0, 0), (-1, -1), 1.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6)]
    for title, grp in rep.rates:
        rows.append([_p(escape(title.upper()), "label")] + [""] * 6)
        styles += [("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1)), ("TOPPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 5)]
        for r in grp:
            rows.append([_p(escape(r.name), "cell"), _p(_link(r.latest, r.url), "numb")]
                        + [_p(escape(x), "num") for x in (r.day, r.week, r.month, r.year, r.as_of)])
            styles.append(("LINEBELOW", (0, len(rows) - 1), (-1, len(rows) - 1), 0.4, RULE))
    num_w = 0.78 * inch
    table = Table(rows, colWidths=[WIDTH - 1.25 * inch - 5 * num_w, 1.25 * inch] + [num_w] * 5)
    table.setStyle(TableStyle(styles))
    return story + [table, Spacer(1, 12), _grid(rep.rates_panels)]


def _last_page(rep: Report) -> list:
    story = _header("Markets & calendar", 5) + [_grid(rep.last_panels[:2])]
    fails = [_p("Bank failures", "ptitle"), Spacer(1, 3),
             _p(str(rep.failures_ytd) if rep.failures_ytd is not None else "n/a", "big"),
             _p(f"failures in {rep.today.year}", "sig"), Spacer(1, 6)]
    if rep.recent_failures:
        fails += [_p(f"<b>{f.fail_date:%b %-d}</b> · {escape(f.name)}, {escape(f.city_state)}"
                     f"{deposits_note(f)}", "cell") for f in rep.recent_failures[:8]]
    else:
        fails.append(_p("None so far this year", "sig"))
    fails += [Spacer(1, 6), _p(_link("FDIC failed bank list", FDIC_FAILURES_URL, LINK), "small")]
    fcell = Table([[fails]], colWidths=[CELL_W])
    fcell.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7)]))
    top = Table([[_panel(rep.last_panels[2]), "", fcell]], colWidths=[CELL_W, GAP, CELL_W])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [top, Spacer(1, 10)]
    if rep.coming_up:
        story += [_p("COMING UP", "label"), Spacer(1, 3)]
        ev = Table([[_p(f"<b>{e.when:%a %b %-d}</b>", "cell"), _p(escape(e.label), "cell")] for e in rep.coming_up],
                   colWidths=[1.1 * inch, WIDTH - 1.1 * inch])
        ev.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
        story += [ev, Spacer(1, 8)]
    story.append(_p("<b>Sources:</b> " + _link("FRED (St. Louis Fed)", "https://fred.stlouisfed.org", LINK) + " · "
                    + _link("FDIC", FDIC_FAILURES_URL, LINK) + " · "
                    + _link("DeFiLlama", "https://defillama.com/stablecoins", LINK)
                    + ". Each reading links to its series.", "small"))
    if rep.missing:
        story += [Spacer(1, 8), _p(f"Unavailable today: {escape(', '.join(rep.missing))}", "small")]
    return story


def render_pdf(rep: Report) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=1.05 * inch,
                            bottomMargin=0.55 * inch, title=f"{NAME} {rep.today:%Y-%m-%d}", author=NAME)
    story = _summary(rep) + [PageBreak()] + _rates_page(rep)
    for i, (title, panels) in enumerate(rep.pages, start=3):
        story += [PageBreak()] + _header(title, i) + [_grid(panels)]
    story += [PageBreak()] + _last_page(rep)

    through = f"  ·  Data through {rep.data_through:%b %-d, %Y}" if rep.data_through else ""

    def chrome(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.rect(0, PAGE_H - 0.75 * inch, PAGE_W, 0.75 * inch, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("DejaVu-Bold", 13)
        canvas.drawString(MARGIN, PAGE_H - 0.42 * inch, NAME)
        canvas.setFont("DejaVu", 8.5)
        canvas.setFillColor(colors.HexColor("#c8cdd3"))
        canvas.drawString(MARGIN, PAGE_H - 0.6 * inch, f"{rep.today:%A, %B %-d, %Y}{through}")
        if rep.sample:
            canvas.setFillColor(colors.HexColor("#ffd666"))
            canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 0.42 * inch, "SAMPLE DATA · NOT REAL RATES")
        canvas.setFillColor(MUTED)
        canvas.setFont("DejaVu", 7)
        canvas.drawString(MARGIN, 0.32 * inch, f"{NAME} · {rep.today:%B %-d, %Y}")
        canvas.drawRightString(PAGE_W - MARGIN, 0.32 * inch, f"{doc_.page} / 5")
        canvas.restoreState()

    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()
