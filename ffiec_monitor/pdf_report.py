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
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .report import (AMBER, BANKFIND_HOME, CDR_HOME, GREEN, NAME, RED, SCREEN_NOTE, Bank, ListRow, RatioRow,
                     Report)

INK, MUTED, RULE, SOFT = colors.HexColor("#14181f"), colors.HexColor("#5b626c"), colors.HexColor("#dde0e4"), colors.HexColor("#f5f6f8")
LINK = "#1d5fbf"
PAGE_W, PAGE_H = letter
MARGIN = 0.5 * inch
WIDTH = PAGE_W - 2 * MARGIN


def _st(name, size, color=INK, bold=False, **kw) -> ParagraphStyle:
    kw.setdefault("leading", size * 1.3)
    return ParagraphStyle(name, fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size, textColor=color, **kw)


S = {
    # Sized up for reading on screen and in print (was ~1.5pt smaller across the board).
    "h": _st("h", 16, bold=True), "note": _st("note", 10, MUTED), "label": _st("label", 8.5, MUTED, bold=True),
    "cell": _st("cell", 10), "cellb": _st("cellb", 10, bold=True), "small": _st("small", 8.5, MUTED),
    "num": _st("num", 10, alignment=TA_RIGHT), "numb": _st("numb", 10, bold=True, alignment=TA_RIGHT),
    "labr": _st("labr", 8.5, MUTED, bold=True, alignment=TA_RIGHT), "tile": _st("tile", 22, bold=True),
    "body": _st("body", 11, leading=15), "card": _st("card", 13, bold=True),
}


def _p(text: str, style: str) -> Paragraph:
    return Paragraph(text, S[style])


def _a(text: str, url) -> str:
    return f'<link href="{escape(url)}" color="{LINK}"><u>{escape(text)}</u></link>' if url else escape(text)


def _links(b: Bank) -> str:
    return " · ".join(x for x in (_a("Call Report", b.call_report_url) if b.call_report_url else "",
                                  _a("UBPR", b.ubpr_url) if b.ubpr_url else "",
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
    for cid, title in (("activity", "Filings logged per day, last 21 days"),
                       ("flags", "Today's new filings: what was flagged")):
        if cid in rep.images:
            img = Image(io.BytesIO(rep.images[cid]))
            img.drawWidth, img.drawHeight = WIDTH, WIDTH * img.imageHeight / img.imageWidth
            out.append(KeepTogether([_p(f"<b>{escape(title)}</b>", "card"), Spacer(1, 4), img, Spacer(1, 12)]))
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
        from .narrative import RECIPROCAL_NOTE
        active = [m for m in rep.enforcement if not m.action.stale]
        stale = [m for m in rep.enforcement if m.action.stale]
        out += _section("Banks under enforcement orders", coverage_note(rep))
        for m in active:
            note, by = rep.enforcement_notes.get(m.bank.rssd, ("", ""))
            issued = f" · issued {m.action.start:%b %-d, %Y}" if m.action.start else ""
            block = [_p(f"<b>{escape(m.bank.name)}</b>", "cell"),
                     _p(f"{escape(m.bank.location)} · {escape(m.bank.assets)} · <b>{escape(m.action.agency)}</b> "
                        f"{escape(m.action.action)}{escape(issued)} · {escape(m.action.topic)} · "
                        f"{_a('Order', m.action.url)} · {_links(m.bank)}", "note")]
            if note:
                block += [Spacer(1, 2), _p(escape(note), "body")]
            card = Table([[block]], colWidths=[WIDTH])
            card.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(RED)),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 4),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            out += [KeepTogether([card]), Spacer(1, 6)]
        if active:
            out.append(_p(escape(RECIPROCAL_NOTE), "note"))
        else:
            out.append(_p("No bank in this report is under an active Fed, OCC or FDIC order or a recent FinCEN action.", "cell"))
        if stale:
            out += [Spacer(1, 4), _p("Not counted: " + "; ".join(
                f"{escape(m.bank.name)} ({escape(m.action.agency)}, {m.action.start:%Y}) has no termination date in the "
                f"data but is over 10 years old, so it is most likely closed. {_a('Check', m.action.url)}" for m in stale), "note")]
    return out


def _performance(rep: Report) -> list:
    from .report import PERF_NOTE
    if not rep.performance:
        return []
    out = _section("How the flagged banks are performing", PERF_NOTE)
    for bp in rep.performance:
        head = [_p("RATIO", "label"), _p(escape(bp.prior_label.upper()), "labr"), _p(escape(bp.bank.period.upper()), "labr"),
                _p("CHANGE", "labr")]
        body = []
        for r in bp.rows:
            if r.current is None and r.prior is None:
                continue
            ch = "" if r.change is None else f"{'+' if r.change >= 0 else '-'}{abs(r.change):.2f}"
            color = GREEN if r.better is True else RED if r.better is False else "#5b626c"
            body.append([_p(escape(r.label), "cell"), _p("" if r.prior is None else f"{r.prior:.2f}%", "num"),
                         _p("" if r.current is None else f"{r.current:.2f}%", "numb"),
                         _p(f"<font color='{color}'>{ch}</font>", "num")])
        out.append(KeepTogether([
            _p(f"<b>{escape(bp.bank.name)}</b>", "cell"),
            _p(f"{escape(bp.bank.location)} · {escape(bp.bank.assets)} · {escape('; '.join(bp.why))} · {_links(bp.bank)}", "note"),
            Spacer(1, 2), _p(escape(bp.read), "cell"), Spacer(1, 3),
            _table([head] + body, [WIDTH - 3.3 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch]), Spacer(1, 10)]))
    return out


PAGES = 5
TOP, BOTTOM = 1.05 * inch, 0.6 * inch
FRAME_H = PAGE_H - TOP - BOTTOM - 14          # small allowance for platypus frame padding


def _header(title: str, n: int) -> list:
    t = Table([[_p(escape(title), "h"), _p(f"{n} / {PAGES}", "labr")]], colWidths=[WIDTH * 0.8, WIDTH * 0.2])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.5, INK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return [t, Spacer(1, 8)]


def _height(flowables: list) -> float:
    return sum(f.wrap(WIDTH, PAGE_H)[1] + getattr(f, "getSpaceBefore", lambda: 0)() + getattr(f, "getSpaceAfter", lambda: 0)()
               for f in flowables)


def _fit(build, start: int) -> list:
    """build(cap) -> flowables; largest cap (<= start) whose page fits on one page."""
    for cap in range(start, -1, -1):
        page = build(cap)
        if _height(page) <= FRAME_H:
            return page
    return build(0)


def _more(shown: int, total: int, where: str = "the attached CSV") -> list:
    return [Spacer(1, 4), _p(f"Showing {shown} of {total}. All {total} are in {where}.", "note")] if total > shown else []


def _chart(rep: Report, cid: str, title: str, height_in: float) -> list:
    if cid not in rep.images:
        return []
    img = Image(io.BytesIO(rep.images[cid]))
    w = WIDTH
    h = w * img.imageHeight / img.imageWidth
    if h > height_in * inch:                     # keep proportions; shrink to the height budget
        h = height_in * inch
        w = h * img.imageWidth / img.imageHeight
    img.drawWidth, img.drawHeight = w, h
    return [_p(f"<b>{escape(title)}</b>", "cell"), Spacer(1, 3), img, Spacer(1, 8)]


# ------------------------------------------------------------------ the five pages
def _page_summary(rep: Report) -> list:
    from .report import tile_specs
    specs = tile_specs(rep.counts)
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
    out = _header("Summary", 1) + [row, Spacer(1, 10)]
    a = rep.analytics
    if a is not None and a.period_end and a.universe:
        left = a.days_left
        due = f" ({left} days left)" if left and left > 1 else ""
        out += [_p(f"{escape(a.quarter.upper())} FILING SEASON", "label"), Spacer(1, 2),
                _p(f"<b>{a.filed:,}</b> of {a.universe:,} banks have filed ({a.pct:.0f}%) · due {a.deadline:%a %b %-d}{due}", "cell"),
                Spacer(1, 4)]
        done = max(min(a.pct, 100), 0.5) / 100 * WIDTH
        bar = Table([["", ""]], colWidths=[done, WIDTH - done], rowHeights=[7])
        bar.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#2a78d6")),
                                 ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#e6e8eb"))]))
        out += [bar, Spacer(1, 10)]
    if rep.summary:
        box = Table([[[_p("WHAT'S GOING ON", "label"), Spacer(1, 3), _p(escape(rep.summary), "body")]]], colWidths=[WIDTH])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("LINEBEFORE", (0, 0), (0, -1), 2.5, INK),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                 ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        out += [box, Spacer(1, 10)]
    room = (FRAME_H - _height(out)) / inch - 0.5
    return out + _chart(rep, "activity", "Filings logged per day, last 21 days", max(1.2, min(3.4, room)))


def _page_risk(rep: Report):
    """Page 2: enforcement orders (with the discussion) and capital."""
    from .narrative import RECIPROCAL_NOTE
    from .report import CBLR_NOTE, LTWC_NOTE, coverage_note
    active = [m for m in rep.enforcement if not m.action.stale]
    stale = [m for m in rep.enforcement if m.action.stale]

    def build(cap):
        out = _header("Enforcement orders and capital", 2)
        if rep.enforcement_sources is not None:
            out += [_p("<b>BANKS UNDER ENFORCEMENT ORDERS</b>", "label"), Spacer(1, 2), _p(escape(coverage_note(rep)), "small"),
                    Spacer(1, 6)]
            for m in active[:cap]:
                note, _by = rep.enforcement_notes.get(m.bank.rssd, ("", ""))
                issued = f" · issued {m.action.start:%b %-d, %Y}" if m.action.start else ""
                block = [_p(f"<b>{escape(m.bank.name)}</b> · {escape(m.bank.location)} · {escape(m.bank.assets)}", "cell"),
                         _p(f"<b>{escape(m.action.agency)}</b> {escape(m.action.action)}{escape(issued)} · "
                            f"{escape(m.action.topic)} · {_a('Order', m.action.url)} · {_links(m.bank)}", "small")]
                if note:
                    block += [Spacer(1, 2), _p(escape(note), "cell")]
                card = Table([[block]], colWidths=[WIDTH])
                card.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(RED)),
                                          ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 3),
                                          ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
                out += [card, Spacer(1, 5)]
            if active:
                out += _more(min(cap, len(active)), len(active), "the email") + [_p(escape(RECIPROCAL_NOTE), "small")]
            else:
                out.append(_p("No bank in this report is under an active Fed, OCC or FDIC order or a recent FinCEN action.", "cell"))
            if stale:
                out += [Spacer(1, 3), _p("Not counted: " + "; ".join(
                    f"{escape(m.bank.name)} ({escape(m.action.agency)}, {m.action.start:%Y}): over 10 years old with no "
                    f"termination date, most likely closed" for m in stale), "small")]
        if rep.capital_checked:
            out += [Spacer(1, 10), _p("<b>LESS THAN WELL CAPITALIZED</b>", "label"), Spacer(1, 2), _p(escape(LTWC_NOTE), "small"),
                    Spacer(1, 4)]
            if rep.ltwc:
                body = [[_bank(b), _p("<br/>".join(escape(x) for x in rs), "cell"), _p(_links(b), "small")]
                        for b, rs in rep.ltwc[:cap]]
                out += [_table([[_p("INSTITUTION", "label"), _p("BELOW THE LINE", "label"), _p("LINKS", "label")]] + body,
                               [2.4 * inch, WIDTH - 3.8 * inch, 1.4 * inch])] + _more(min(cap, len(rep.ltwc)), len(rep.ltwc))
            else:
                out.append(_p("None. Every filing with capital data is at or above the well-capitalized minimums.", "cell"))
            if rep.cblr:
                out += [Spacer(1, 8), _p(f"<b>COMMUNITY BANK LEVERAGE RATIO BANKS UNDER 9% ({len(rep.cblr)})</b>", "label"),
                        Spacer(1, 2), _p(escape(CBLR_NOTE), "small"), Spacer(1, 3),
                        _p(" · ".join(f"{escape(b.name)} ({escape(b.location.split(',')[-1].strip())}) "
                                      f"{rs[0].split('%')[0].replace('Leverage ratio ', '')}%" for b, rs in rep.cblr[:cap * 3]),
                           "cell")] + _more(min(cap * 3, len(rep.cblr)), len(rep.cblr))
        return out
    return _fit(build, 8)


def _page_performance(rep: Report):
    """Page 3: one row per flagged bank, this quarter vs. last."""
    keys = [("Leverage ratio", "LEVERAGE"), ("Return on assets", "ROA"), ("Noncurrent loans + OREO", "PROBLEM LOANS"),
            ("Loans to deposits", "LOANS/DEP."), ("Brokered/reciprocal share", "BROK./RECIP.")]
    perf = sorted(rep.performance, key=lambda bp: ("Mostly worsening" not in bp.read, "order" not in " ".join(bp.why)))

    def cell(bp, label):
        r = next((x for x in bp.rows if x.label == label), None)
        if r is None or r.current is None:
            return _p("", "num")
        ch = "" if r.change is None else f"<br/><font size='8' color='{GREEN if r.better else RED if r.better is False else '#5b626c'}'>" \
                                         f"{'+' if r.change >= 0 else '-'}{abs(r.change):.2f}</font>"
        return _p(f"{r.current:.2f}%{ch}", "num")

    def build(cap):
        from .report import PERF_NOTE
        out = _header("How the flagged banks are performing", 3) + [_p(escape(PERF_NOTE), "note"), Spacer(1, 6)]
        if not perf:
            return out + [_p("No bank in this report is under an order, below well capitalized, or a CBLR bank under 9%.", "cell")]
        head = [_p("BANK", "label"), _p("WHY", "label")] + [_p(h, "labr") for _, h in keys]
        body = [[_p(f"<b>{escape(bp.bank.name)}</b><br/><font size='8' color='#5b626c'>{escape(bp.bank.location)} · "
                    f"{escape(bp.bank.assets)}</font>", "cell"),
                 _p(f"{escape('; '.join(bp.why))}<br/><font size='8' color='#5b626c'>{escape(bp.read.split(':')[0])}</font>", "small")]
                + [cell(bp, k) for k, _ in keys] for bp in perf[:cap]]
        w = 0.82 * inch
        out.append(_table([head] + body, [WIDTH - 1.55 * inch - 5 * w, 1.55 * inch] + [w] * 5))
        return out + _more(min(cap, len(perf)), len(perf), "the email") + [
            Spacer(1, 4), _p(f"Change vs. {perf[0].prior_label} under each value. Full nine-ratio comparison per bank is in the email.",
                             "small")]
    return _fit(build, 30)


def _page_flags(rep: Report):
    """Page 4: new filings the screen flagged."""
    def build(cap):
        out = _header("New filings to review", 4) + [_p(escape(SCREEN_NOTE), "note"), Spacer(1, 6)]
        out += _chart(rep, "flags", "Today's new filings: what was flagged", 2.3 if cap > 6 else 1.6)
        if not rep.flagged:
            return out + [_p("No new filing has a red flag.", "cell")]
        body = [[_bank(r.bank), _p("<br/>".join(f"<font color='{RED if f.level == 'concern' else AMBER}'>•</font> {escape(f.text)}"
                                                 for f in r.flags[:3]), "cell"), _p(_links(r.bank), "small")]
                for r in rep.flagged[:cap]]
        out.append(_table([[_p("INSTITUTION", "label"), _p("WHAT STANDS OUT (TOP 3)", "label"), _p("LINKS", "label")]] + body,
                          [2.3 * inch, WIDTH - 3.7 * inch, 1.4 * inch]))
        return out + _more(min(cap, rep.flagged_total), rep.flagged_total, "the attached CSV (Flags column)")
    return _fit(build, 20)


def _page_rest(rep: Report):
    """Page 5: alerts and Watch List (when any), refilings, other new filings, sources."""
    def build(cap):
        out = _header("Alerts, refilings and other filings", 5)
        if rep.tier1 or rep.tier2 or rep.watch_changes:
            rows = ([[_p("<b>Tier 1</b>", "cell"), _bank(a.bank), _p("<br/>".join(escape(t) for t in a.triggers[:2]), "cell"),
                      _p(_links(a.bank), "small")]
                     for a in rep.tier1[:cap]]
                    + [[_p("Tier 2", "cell"), _bank(a.bank), _p("<br/>".join(escape(t) for t in a.triggers[:2]), "cell"),
                      _p(_links(a.bank), "small")]
                       for a in rep.tier2[:cap]]
                    + [[_p(f"Watch List: {escape(w.verb.lower())}", "cell"), _bank(w.bank), _p(escape(w.reason), "cell"),
                      _p(_links(w.bank), "small")]
                       for w in rep.watch_changes[:cap]])
            out += [_p("<b>ALERTS</b> (full detail and ratio tables in the email)", "label"), Spacer(1, 3),
                    _table([[_p("TYPE", "label"), _p("INSTITUTION", "label"), _p("WHAT HAPPENED", "label"),
                             _p("LINKS", "label")]] + rows,
                           [1.2 * inch, 2.2 * inch, WIDTH - 4.8 * inch, 1.4 * inch]), Spacer(1, 10)]
        if rep.refilings:
            body = [[_bank(r.bank), _p(escape(r.bank.period), "cell"), _p(escape(r.detail), "cell"), _p(_links(r.bank), "small")]
                    for r in rep.refilings[:cap]]
            out += [_p(f"<b>REFILINGS ({rep.refilings_total})</b>, largest first", "label"), Spacer(1, 3),
                    _table([[_p("INSTITUTION", "label"), _p("PERIOD", "label"), _p("WHAT CHANGED", "label"), _p("LINKS", "label")]] + body,
                           [2.3 * inch, 0.7 * inch, WIDTH - 4.4 * inch, 1.4 * inch])]
            out += _more(min(cap, rep.refilings_total), rep.refilings_total) + [Spacer(1, 10)]
        if rep.new_filings_total:
            names = ", ".join(f"{escape(r.bank.name)} ({escape(r.bank.assets)})" for r in rep.new_filings[:cap])
            out += [_p(f"<b>OTHER NEW FILINGS ({rep.new_filings_total})</b>, no red flags; largest first", "label"), Spacer(1, 3),
                    _p(names, "cell")] + _more(min(cap, rep.new_filings_total), rep.new_filings_total) + [Spacer(1, 10)]
        out += [_p(f"<b>Sources:</b> {_a('FFIEC Central Data Repository', CDR_HOME)} (Call Reports) · "
                   f"{_a('FDIC BankFind', BANKFIND_HOME)} (institution profiles) · Fed, OCC and FinCEN enforcement pages · "
                   "FDIC Enforcement Decisions and Orders. Every bank and filing is in the attached CSV.", "small")]
        return out
    return _fit(build, 15)


def render_pdf(rep: Report) -> bytes:
    """Always exactly five pages: each page is fitted to one page (long lists are cut, with a count,
    and are complete in the email and the CSV)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=TOP,
                            bottomMargin=BOTTOM, title=f"{NAME} {rep.run_date:%Y-%m-%d}", author=NAME)
    pages = [_fit(lambda _cap: _page_summary(rep), 0), _page_risk(rep), _page_performance(rep), _page_flags(rep),
             _page_rest(rep)]
    story: list = []
    for i, page in enumerate(pages):
        story += page + ([PageBreak()] if i < len(pages) - 1 else [])

    def chrome(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(INK)
        canvas.rect(0, PAGE_H - 0.75 * inch, PAGE_W, 0.75 * inch, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 15)
        canvas.drawString(MARGIN, PAGE_H - 0.42 * inch, NAME)
        canvas.setFont("Helvetica", 10)
        canvas.setFillColor(colors.HexColor("#c8cdd3"))
        canvas.drawString(MARGIN, PAGE_H - 0.6 * inch, f"{rep.edition or f'{rep.run_date:%A, %B %-d, %Y}'}  ·  {rep.status}"[:120])
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(MARGIN, 0.35 * inch, f"{NAME} · {rep.run_date:%B %-d, %Y}")
        canvas.drawRightString(PAGE_W - MARGIN, 0.35 * inch, f"{doc_.page} / {PAGES}")
        canvas.restoreState()

    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()
