"""The daily report as a PDF attachment: same content and order as the HTML email
(ffiec_monitor/report.py). Letter size; length follows the day's activity, with long
lists capped at report.LIST_CAP rows (largest institutions first; the CSV has everything).
Every institution links to its Call Report and FDIC profile.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .report import (AMBER, BANKFIND_HOME, CDR_HOME, NAME, RED, SCREEN_NOTE, Bank, ListRow, RatioRow,
                     Report)

INK, MUTED, RULE, SOFT = colors.HexColor("#14181f"), colors.HexColor("#5b626c"), colors.HexColor("#dde0e4"), colors.HexColor("#f5f6f8")
LINK = "#1d5fbf"
PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
WIDTH = PAGE_W - 2 * MARGIN


def _st(name, size, color=INK, bold=False, **kw) -> ParagraphStyle:
    kw.setdefault("leading", size * 1.3)
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size, textColor=color, **kw)


S = {
    "h": _st("h", 14, bold=True), "note": _st("note", 8.5, MUTED), "label": _st("label", 7.5, MUTED, bold=True),
    "cell": _st("cell", 8.5), "cellb": _st("cellb", 8.5, bold=True), "small": _st("small", 7.5, MUTED),
    "num": _st("num", 8.5, alignment=TA_RIGHT), "numb": _st("numb", 8.5, bold=True, alignment=TA_RIGHT),
    "labr": _st("labr", 7.5, MUTED, bold=True, alignment=TA_RIGHT), "tile": _st("tile", 18, bold=True),
    "body": _st("body", 9.2, leading=12.6), "card": _st("card", 11.5, bold=True),
}


def _p(text: str, style: str) -> Paragraph:
    return Paragraph(text, S[style])


def _a(text: str, url) -> str:
    return f'<link href="{escape(url)}" color="{LINK}"><u>{escape(text)}</u></link>' if url else escape(text)


def _links(b: Bank) -> str:
    return " · ".join(x for x in (_a("Call Report", b.call_report_url) if b.call_report_url else "",
                                  _a("FDIC profile", b.bankfind_url) if b.bankfind_url else "") if x)


def _bank(b: Bank) -> Paragraph:
    return _p(f"<b>{escape(b.name)}</b><br/><font size='7' color='#5b626c'>{escape(b.location)} · {escape(b.assets)}</font>", "cell")


def _table(rows, widths, extra=None) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)
    style = [("LINEBELOW", (0, 0), (-1, 0), 1, INK), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
             ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    style += [("LINEBELOW", (0, i), (-1, i), 0.4, RULE) for i in range(1, len(rows))]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def _section(title: str, note: str = "") -> list:
    h = Table([[_p(escape(title), "h")]], colWidths=[WIDTH])
    h.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1.4, INK), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    out = [Spacer(1, 14), h]
    if note:
        out += [Spacer(1, 3), _p(escape(note), "note")]
    return out + [Spacer(1, 6)]


def _ratio_table(rows: list[RatioRow], before: str = "BEFORE") -> Table:
    peer = any(r.peer for r in rows)
    head = [_p("RATIO", "label"), _p(before, "labr"), _p("AFTER", "labr"), _p("CHANGE", "labr")] + ([_p("PEER AVG", "labr")] if peer else [])
    body = [[_p(("&nbsp;&nbsp;&nbsp;" if r.sub else "") + escape(r.label), "cell"), _p(escape(r.before), "num"),
             _p(escape(r.after), "numb"), _p(escape(r.change), "num")] + ([_p(escape(r.peer), "num")] if peer else [])
            for r in rows]
    n = 4 + peer
    w = 0.95 * inch
    return _table([head] + body, [WIDTH - 24 - w * (n - 1)] + [w] * (n - 1))


def _current_table(rows: list[RatioRow]) -> Table:
    head = [_p("RATIO", "label"), _p("CURRENT", "labr"), _p("PEER AVG", "labr")]
    body = [[_p(("&nbsp;&nbsp;&nbsp;" if r.sub else "") + escape(r.label), "cell"), _p(escape(r.after), "numb"),
             _p(escape(r.peer), "num")] for r in rows]
    return _table([head] + body, [WIDTH - 2.4 * inch, 1.1 * inch, 1.1 * inch])


def _list(rows: list[ListRow], total: int, detail: bool) -> list:
    head = [_p("INSTITUTION", "label"), _p("PERIOD", "label")] + ([_p("WHAT CHANGED", "label")] if detail else []) + [_p("LINKS", "label")]
    body = [[_bank(r.bank), _p(escape(r.bank.period), "cell")] + ([_p(escape(r.detail), "cell")] if detail else [])
            + [_p(_links(r.bank), "small")] for r in rows]
    widths = ([2.5 * inch, 0.7 * inch, WIDTH - 2.5 * inch - 0.7 * inch - 1.45 * inch, 1.45 * inch] if detail
              else [WIDTH - 0.8 * inch - 1.45 * inch, 0.8 * inch, 1.45 * inch])
    out = [_table([head] + body, widths)]
    if total > len(rows):
        out += [Spacer(1, 4), _p(f"Showing the {len(rows)} largest of {total}. All {total} are in the attached CSV.", "note")]
    return out


def _glance(rep: Report) -> list:
    """Filing-season progress bar and the two charts, under the count tiles."""
    out: list = []
    a = rep.analytics
    if a is not None and a.period_end and a.universe:
        left = a.days_left
        due = f" ({left} days left)" if left and left > 1 else ""
        out += [_p(f"{escape(a.quarter.upper())} FILING SEASON", "label"), Spacer(1, 2),
                _p(f"<b>{a.filed:,}</b> of {a.universe:,} banks have filed ({a.pct:.0f}%) · due {a.deadline:%a %b %-d}{due}",
                   "cell"), Spacer(1, 4)]
        done = max(min(a.pct, 100), 0.5) / 100 * WIDTH
        bar = Table([["", ""]], colWidths=[done, WIDTH - done], rowHeights=[7])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#2a78d6")),
                                 ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#e6e8eb"))]))
        out += [bar, Spacer(1, 10)]
    cells = []
    half = (WIDTH - 14) / 2
    for cid, title in (("activity", "Filings logged per day, last 21 days"),
                       ("flags", "Today's new filings: what was flagged")):
        if cid in rep.images:
            img = Image(io.BytesIO(rep.images[cid]))
            img.drawWidth, img.drawHeight = half, half * img.imageHeight / img.imageWidth
            cells.append([_p(f"<b>{escape(title)}</b>", "cell"), Spacer(1, 3), img])
    if cells:
        row = [cells[0], "", cells[1]] if len(cells) == 2 else [cells[0], "", ""]
        t = Table([row], colWidths=[half, 14, half])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        out += [t, Spacer(1, 10)]
    return out


def _risk_sections(rep: Report) -> list:
    from .report import coverage_note
    out: list = []
    if rep.capital_checked:
        from .report import CBLR_NOTE, LTWC_NOTE
        out += _section("Less than well capitalized", LTWC_NOTE)
        if rep.ltwc:
            head = [_p("INSTITUTION", "label"), _p("PERIOD", "label"), _p("BELOW THE LINE", "label"), _p("LINKS", "label")]
            body = [[_bank(b), _p(escape(b.period), "cell"), _p("<br/>".join(escape(x) for x in rs), "cell"),
                     _p(_links(b), "small")] for b, rs in rep.ltwc]
            out.append(_table([head] + body, [2.2 * inch, 0.65 * inch, WIDTH - 4.25 * inch, 1.4 * inch]))
        else:
            out.append(_p("None. Every filing with capital data is at or above the well-capitalized lines.", "cell"))
        if rep.cblr:
            out += _section("Community bank leverage ratio banks under 9%", CBLR_NOTE)
            head = [_p("INSTITUTION", "label"), _p("PERIOD", "label"), _p("LEVERAGE", "label"), _p("LINKS", "label")]
            body = [[_bank(b), _p(escape(b.period), "cell"), _p(escape(rs[0]), "cell"), _p(_links(b), "small")]
                    for b, rs in rep.cblr]
            out.append(_table([head] + body, [2.2 * inch, 0.65 * inch, WIDTH - 4.25 * inch, 1.4 * inch]))
    if rep.enforcement_sources is not None:
        out += _section("Enforcement actions", coverage_note(rep))
        if rep.enforcement:
            head = [_p("INSTITUTION", "label"), _p("AGENCY", "label"), _p("ACTION", "label"), _p("SINCE", "label"),
                    _p("LINKS", "label")]
            body = [[_bank(m.bank), _p(f"<b>{escape(m.action.agency)}</b><br/><font size='7' color='#5b626c'>"
                                       f"{escape(m.action.topic)}</font>", "cell"),
                     _p(f"{escape(m.action.action)}<br/><font size='7' color='#5b626c'>as listed: {escape(m.action.name)}"
                        f" · matched on {escape(m.how)}"
                        + (" · over 10 years old with no termination date: confirm it is still in force" if m.action.stale else "")
                        + "</font>", "cell"),
                     _p(f"{m.action.start:%b %-d, %Y}" if m.action.start else "", "cell"),
                     _p(_a("Source", m.action.url) + " · " + _links(m.bank), "small")] for m in rep.enforcement]
            out.append(_table([head] + body, [2.0 * inch, 0.6 * inch, WIDTH - 5.0 * inch, 0.9 * inch, 1.5 * inch]))
        else:
            out.append(_p("No bank in this report matched an active Fed or OCC order or agreement or a recent FinCEN action. "
                          + _a("Search FDIC orders", "https://orders.fdic.gov/s/"), "cell"))
    return out


def render_pdf(rep: Report) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=1.05 * inch,
                            bottomMargin=0.6 * inch, title=f"{NAME} {rep.run_date:%Y-%m-%d}", author=NAME)
    c = rep.counts
    from .report import tile_specs
    specs = tile_specs(c)
    tw = (WIDTH - (len(specs) - 1) * 6) / len(specs)
    tiles = []
    for label, n, color in specs:
        t = Table([[_p(str(n), "tile")], [_p(label, "label")]], colWidths=[tw])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("LINEABOVE", (0, 0), (-1, 0), 3.5, colors.HexColor(color)),
                               ("LEFTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (0, 0), 6),
                               ("BOTTOMPADDING", (0, -1), (-1, -1), 6)]))
        tiles += [t, ""]
    row = Table([tiles[:-1]], colWidths=[tw, 6] * (len(specs) - 1) + [tw])
    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story: list = [row, Spacer(1, 12)] + _glance(rep)
    if rep.summary:
        inner = [_p("WHAT'S GOING ON", "label"), Spacer(1, 3), _p(escape(rep.summary), "body")]
        if rep.summary_by == "Claude":
            inner += [Spacer(1, 3), _p("Written by Claude from the filings in this report.", "small")]
        box = Table([[inner]], colWidths=[WIDTH])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LINEBEFORE", (0, 0), (0, -1), 2.5, INK),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                 ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        story.append(box)

    story += _risk_sections(rep)

    if rep.watch_changes:
        story += _section("Watch List changes")
        head = [_p("CHANGE", "label"), _p("INSTITUTION", "label"), _p("REASON", "label"), _p("LINKS", "label")]
        body = [[_p(f"<b>{escape(w.verb)}</b>", "cell"), _bank(w.bank), _p(escape(w.reason), "cell"), _p(_links(w.bank), "small")]
                for w in rep.watch_changes]
        story.append(_table([head] + body, [0.9 * inch, 2.4 * inch, WIDTH - 4.75 * inch, 1.45 * inch]))

    if rep.tier1:
        head = _section("Tier 1 alerts", "A capital, earnings or asset-quality threshold was crossed. Review today.")
        for i, a in enumerate(rep.tier1):
            b = a.bank
            content = [_p(escape(b.name) + (" <font color='#8a5a00' size='8'>WATCH LIST</font>" if a.on_watch_list else ""), "card"),
                       _p(f"{escape(b.location)} · {escape(b.assets)} assets · {escape(b.period)} · FDIC Cert {escape(b.cert or 'n/a')} · "
                          f"RSSD {escape(b.rssd)} · {_links(b)}", "note"), Spacer(1, 5)]
            content += [_p(f"• {escape(t)}", "cell") for t in a.triggers]
            if a.ratios:
                content += [Spacer(1, 5), _ratio_table(a.ratios)]
            if a.snapshot:
                content += [Spacer(1, 8), _p("ALL CURRENT RATIOS (WATCH LIST BANK)", "label"), Spacer(1, 2), _current_table(a.snapshot)]
            card = Table([[content]], colWidths=[WIDTH])
            card.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, RULE), ("LINEABOVE", (0, 0), (-1, 0), 3.5, colors.HexColor(RED)),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                                      ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
            # keep the section heading with its first card
            story += [KeepTogether(head + [card]), Spacer(1, 8)] if i == 0 else [card, Spacer(1, 8)]

    if rep.snapshot:
        head = _section("Watch List: current ratios", "Every Watch List bank, every day, whether or not anything changed.")
        for i, a in enumerate(rep.snapshot):
            block = [_p(f"<b>{escape(a.bank.name)}</b>", "cell"),
                     _p(f"{escape(a.bank.location)} · {escape(a.bank.assets)} · {_links(a.bank)}", "note"),
                     Spacer(1, 2), _current_table(a.ratios), Spacer(1, 10)]
            story.append(KeepTogether((head if i == 0 else []) + block))

    if rep.tier2:
        story += _section("Tier 2", "Slower-moving deterioration worth watching.")
        head = [_p("INSTITUTION", "label"), _p("WHAT CHANGED", "label"), _p("LINKS", "label")]
        body = [[_bank(a.bank), _p("<br/>".join(escape(t) for t in a.triggers), "cell"), _p(_links(a.bank), "small")] for a in rep.tier2]
        story.append(_table([head] + body, [2.4 * inch, WIDTH - 3.85 * inch, 1.45 * inch]))

    if rep.flagged:
        story += _section("New filings to review", SCREEN_NOTE)
        head = [_p("INSTITUTION", "label"), _p("PERIOD", "label"), _p("WHAT STANDS OUT", "label"), _p("LINKS", "label")]
        body = [[_bank(r.bank), _p(escape(r.bank.period), "cell"),
                 _p("<br/>".join(f"<font color='{RED if f.level == 'concern' else AMBER}'>•</font> {escape(f.text)}"
                                 for f in r.flags), "cell"), _p(_links(r.bank), "small")] for r in rep.flagged]
        story.append(_table([head] + body, [2.2 * inch, 0.65 * inch, WIDTH - 2.2 * inch - 0.65 * inch - 1.4 * inch, 1.4 * inch]))
        if rep.flagged_total > len(rep.flagged):
            story += [Spacer(1, 4), _p(f"Showing {len(rep.flagged)} of {rep.flagged_total}. All are in the CSV (Flags column).", "note")]

    if rep.refilings:
        story += _section("Refilings", "Amended Call Reports where no capital ratio moved more than 0.50 points.")
        story += _list(rep.refilings, rep.refilings_total, True)
    if rep.new_filings:
        story += _section("Other new filings", "First filing for the quarter; no threshold crossed and no red flags "
                          "(amber flags are in the CSV).")
        story += _list(rep.new_filings, rep.new_filings_total, False)

    story += [Spacer(1, 16), _p(f"<b>Sources:</b> {_a('FFIEC Central Data Repository', CDR_HOME)} (Call Reports) · "
                                f"{_a('FDIC BankFind', BANKFIND_HOME)} (institution profiles). The full list is in the CSV "
                                "attached to the email.", "small")]

    def chrome(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.rect(0, PAGE_H - 0.75 * inch, PAGE_W, 0.75 * inch, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 13)
        canvas.drawString(MARGIN, PAGE_H - 0.42 * inch, NAME)
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#c8cdd3"))
        canvas.drawString(MARGIN, PAGE_H - 0.6 * inch, f"{rep.edition or f'{rep.run_date:%A, %B %-d, %Y}'}  ·  {rep.status}")
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(MARGIN, 0.35 * inch, f"{NAME} · {rep.run_date:%B %-d, %Y}")
        canvas.drawRightString(PAGE_W - MARGIN, 0.35 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()
