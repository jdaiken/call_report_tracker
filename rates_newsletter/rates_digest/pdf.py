"""The PDF attachment: the same Report as the email, laid out for printing or
forwarding (US Letter)."""
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
from reportlab.platypus import (CondPageBreak, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from .newsletter import TONE_COLOR, TONE_WORD, ItemContent, Report

# DejaVu ships with matplotlib, so the PDF needs no system fonts and renders
# symbols like the minus sign that the built-in PDF fonts lack.
_FONT_DIR = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf")
pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(_FONT_DIR, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")

INK, QUIET, RULE, SHADE = colors.HexColor("#1f2328"), colors.HexColor("#5f6670"), colors.HexColor("#d9dce0"), colors.HexColor("#f4f5f7")


def _style(name, size=9.5, leading=None, color=INK, bold=False, **kw) -> ParagraphStyle:
    return ParagraphStyle(name, fontName="DejaVu-Bold" if bold else "DejaVu", fontSize=size,
                          leading=leading or size * 1.35, textColor=color, **kw)


S = {
    "kicker": _style("kicker", 8, color=QUIET),
    "title": _style("title", 17, bold=True, spaceAfter=6),
    "h2": _style("h2", 13, bold=True, spaceBefore=10, spaceAfter=2),
    "q": _style("q", 9, color=QUIET, spaceAfter=4),
    "body": _style("body", 9.5),
    "small": _style("small", 8, color=QUIET),
    "take": _style("take", 10.5, bold=True, spaceBefore=6),
    "name": _style("name", 10.5, bold=True),
    "reading": _style("reading", 11, bold=True, alignment=TA_RIGHT),
    "badge": _style("badge", 7.5, bold=True, color=colors.white),
}


def _p(text: str, style: str) -> Paragraph:
    return Paragraph(text, S[style])


def _badge(tone: str) -> Table:
    t = Table([[_p(TONE_WORD[tone].upper(), "badge")]], colWidths=[0.62 * inch])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(TONE_COLOR[tone])),
                           ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                           ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _png(png: bytes, width: float) -> Image:
    img = Image(io.BytesIO(png))
    ratio = img.imageHeight / img.imageWidth
    img.drawWidth, img.drawHeight = width, width * ratio
    return img


def _item(i: ItemContent, width: float) -> KeepTogether:
    color = TONE_COLOR[i.tone] if i.tone in ("watch", "risk") else "#1f2328"
    head = Table([[_badge(i.tone), _p(escape(i.name), "name"), _p(escape(i.reading), "reading")]],
                 colWidths=[0.72 * inch, width - 2.2 * inch, 1.48 * inch])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    parts = [Spacer(1, 6), head]
    meta = " · ".join(x for x in (f"as of {i.as_of}" if i.as_of else "", i.changes,
                                   f"Past year: {i.past_year}" if i.past_year else "") if x)
    if meta:
        parts.append(_p(escape(meta), "small"))
    insight = Table([[Paragraph(f"Insight: {escape(i.insight)}", _style("ins", 10, bold=True, color=colors.HexColor(color)))]],
                    colWidths=[width])
    insight.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SHADE),
                                 ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(color)),
                                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    parts += [Spacer(1, 3), insight, Spacer(1, 3),
              _p(f"<b>Why IntraFi cares:</b> {escape(i.why)}", "body"), Spacer(1, 2),
              _p(f"<b>What it is:</b> {escape(i.what)}", "body")]
    if i.ranges:
        chips = " · ".join(f'<font color="{TONE_COLOR[c.tone]}"><b>» {escape(c.label)}</b></font>' if c.current
                           else escape(c.label) for c in i.ranges)
        basis = f" ({escape(i.basis)})" if i.basis else ""
        parts += [Spacer(1, 2), _p(f"<b>Ranges{basis}:</b> {chips}", "small")]
    parts.append(Spacer(1, 4))
    line = Table([[""]], colWidths=[width], rowHeights=[0.5])
    line.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), 0.5, RULE)]))
    return KeepTogether([line] + parts)


def render_pdf(rep: Report) -> bytes:
    buf = io.BytesIO()
    margin = 0.6 * inch
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=margin, rightMargin=margin, topMargin=margin,
                            bottomMargin=0.65 * inch, title=f"IntraFi Risk Monitor {rep.today:%Y-%m-%d}",
                            author="IntraFi Risk Monitor (automated)")
    width = letter[0] - 2 * margin
    story = []
    if rep.sample:
        story.append(_p("<b>SAMPLE DATA FOR PREVIEW ONLY. These numbers are made up, not real rates.</b>", "body"))
    story += [_p("INTRAFI RISK MONITOR · RATES &amp; MARKETS", "kicker"), _p(f"{rep.today:%A, %B %-d, %Y}", "title")]

    story.append(_p("Today's alerts", "h2"))
    if rep.alerts:
        for t, x in rep.alerts:
            story.append(Paragraph(f"<b>{TONE_WORD[t]}:</b> {escape(x)}",
                                   _style(f"al{t}", 9.5, color=colors.HexColor(TONE_COLOR[t]), leftIndent=8, spaceAfter=3)))
    else:
        story.append(_p("<b>All clear:</b> nothing is in a Watch or Risk range.", "body"))
    story.append(_p(escape(rep.failures_line), "body"))

    story.append(_p("Risk dashboard", "h2"))
    story.append(_p("Green = Normal, amber = Watch, red = Risk, gray = for information. "
                    "Ranges are rules of thumb, not official thresholds.", "small"))
    rows, styles = [], [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    for a in rep.areas:
        styles.append(("TOPPADDING", (0, len(rows)), (-1, len(rows)), 7))
        rows.append([_p(f"<b>{escape(a.title.upper())}</b>", "small"), "", ""])
        styles.append(("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1)))
        for i in a.items:
            styles.append(("LINEBELOW", (0, len(rows)), (-1, len(rows)), 0.4, RULE))
            rows.append([_badge(i.tone), _p(f"<b>{escape(i.name)}</b><br/><font color='#5f6670'>{escape(i.insight)}</font>", "body"),
                         _p(escape(i.reading), "reading")])
    dash = Table(rows, colWidths=[0.72 * inch, width - 2.0 * inch, 1.28 * inch])
    dash.setStyle(TableStyle(styles))
    story.append(dash)

    for a in rep.areas:
        story.append(CondPageBreak(3.2 * inch))
        story.append(_p(f'{escape(a.title)} <font size="9" color="{TONE_COLOR[a.tone]}">· {TONE_WORD[a.tone].upper()}</font>', "h2"))
        story.append(_p(f"<i>{escape(a.question)}</i>", "q"))
        for c in a.charts:
            story.append(KeepTogether([_p(escape(c.takeaway), "take"), _p(escape(c.subtitle), "small"),
                                       Spacer(1, 2), _png(c.png, width * 0.92)]))
        for i in a.items:
            if i.loaded:
                story.append(_item(i, width))
            else:
                story.append(_p(f"<b>{escape(i.name)}</b>: {escape(i.insight)}", "body"))

    if rep.coming_up:
        story += [_p("Coming up", "h2")] + [_p(f"<b>{e.when:%a %b %-d}</b> · {escape(e.label)}", "body") for e in rep.coming_up]
    if rep.missing:
        story += [Spacer(1, 8), _p(f"Did not load today: {escape(', '.join(rep.missing))}.", "small")]
    story += [Spacer(1, 10), _p("Data: FRED (Federal Reserve Bank of St. Louis) republishing the Federal Reserve, New York Fed, "
                                "Treasury, FDIC and ICE BofA; FDIC BankFind; DeFiLlama; federalreserve.gov. Automated summary "
                                "for personal reference, not investment advice.", "small")]

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("DejaVu", 7.5)
        canvas.setFillColor(QUIET)
        canvas.drawString(margin, 0.4 * inch, f"IntraFi Risk Monitor · {rep.today:%B %-d, %Y}")
        canvas.drawRightString(letter[0] - margin, 0.4 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
