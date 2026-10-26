"""Email, four-page PDF and spreadsheet for the weekly Network Exposure report (network.py)."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from .network import HIGH_UNINSURED, NAME, NEAR_CAP, Network, NetBank, _fmt_money, _pct

ROWS = 12
CAP_NOTE = ("Reciprocal deposits are excepted from brokered only up to the lesser of $5 billion or 20% of total liabilities "
            "(12 CFR 337.6(e)). Banks not well capitalized and well rated are held to a special cap, their four-quarter "
            "average; ratings aren't public, so banks below well capitalized or under an enforcement order are shown as "
            "likely on it.")


def _change(nb: NetBank) -> str:
    if nb.prior_used is None or nb.used is None:
        return ""
    d = nb.used - nb.prior_used
    return f"{'+' if d >= 0 else '-'}{abs(d):.1f} pts"


# ------------------------------------------------------------------ email
def render_html(net: Network) -> str:
    from .report import AMBER, GREEN, INK, MUTED, RED, SOFT, TD, TDR, TH, THR, _bank_cell, _e, _links, _section, _tile
    s = net.stats()
    tiles = [("BANKS ANALYZED", f"{s['banks']:,}", "#4b5563"), ("USE RECIPROCAL", f"{s['users']:,}", "#2a78d6"),
             ("RECIPROCAL TOTAL", _fmt_money(s["reciprocal_total"]), "#2a78d6"),
             (f"AT {NEAR_CAP:.0f}%+ OF CAP", f"{s['near_cap'] + s['over_cap']}", AMBER if s["near_cap"] + s["over_cap"] else GREEN),
             ("LIKELY SPECIAL CAP", f"{s['special']}", RED if s["special"] else GREEN),
             (f"UNINSURED {HIGH_UNINSURED:.0f}%+", f"{s['high_uninsured']:,}", "#eb6834")]
    b = ('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
         + '<td width="8"></td>'.join(_tile(lab, val, col) for lab, val, col in tiles) + "</tr></table></td></tr>")
    if net.summary:
        b += (f'<tr><td style="padding:16px 0 0"><div style="background:{SOFT};border-left:3px solid {INK};padding:10px 12px">'
              f'<div style="font-size:12.4px;font-weight:700;letter-spacing:.08em;color:{MUTED}">ANALYSIS</div>'
              f'<div style="font-size:15.9px;line-height:1.5;color:{INK};margin-top:4px">{_e(net.summary)}</div></div></td></tr>')

    def img(cid, title):
        if cid not in net.images:
            return ""
        return (f'<tr><td style="padding:16px 0 0"><div style="font-size:15px;font-weight:700;color:{INK};margin-bottom:6px">{_e(title)}</div>'
                f'<img src="cid:net_{cid}" width="712" alt="{_e(title)}" style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')

    def table(rows, extra_head, extra_cells):
        head = f'<tr><th {TH}>BANK</th><th {THR}>RECIPROCAL</th><th {THR}>% OF CAP</th><th {THR}>UNINSURED</th>{extra_head}</tr>'
        body = "".join(
            f'<tr>{_bank_cell(nb.bank)}<td {TDR}>{_fmt_money(nb.reciprocal)}</td>'
            f'<td {TDR}><b>{_pct(nb.used)}</b><div style="font-size:12px;color:{MUTED}">{_change(nb)}</div></td>'
            f'<td {TDR}>{_pct(nb.uninsured_share)}</td>{extra_cells(nb)}</tr>' for nb in rows)
        return f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{body}</table></td></tr>'

    def more(shown, total):
        return (f'<tr><td style="padding:6px 0 0;font-size:13px;color:{MUTED}">Showing {shown} of {total}. All are in the '
                f'attached spreadsheet.</td></tr>' if total > shown else "")

    b += img("usage", "How much of the reciprocal cap banks use")
    b += img("map", "Exposure map: uninsured deposits vs. reciprocal cap used (bubble = assets)")

    near = net.near_cap()
    b += _section(f"Nearest or over the reciprocal cap ({len(near)})", CAP_NOTE)
    b += (table(near[:ROWS], f'<th {TH}>LINKS</th>', lambda nb: f'<td {TD} nowrap>{_links(nb.bank)}</td>') + more(min(ROWS, len(near)), len(near))
          if near else f'<tr><td style="padding:8px 0;font-size:15px;color:{INK}">No bank is at {NEAR_CAP:.0f}% of its cap or more.</td></tr>')

    spec = net.special()
    b += _section(f"Likely on the special cap ({len(spec)})", "Reciprocal users below well capitalized or under an active "
                  "enforcement order: reciprocal deposits above their four-quarter average would count as brokered.")
    b += (table(spec[:ROWS], f'<th {TH}>WHY</th>', lambda nb: f'<td {TD}>{_e("; ".join(nb.special_cap))}<div>{_links(nb.bank)}</div></td>')
          + more(min(ROWS, len(spec)), len(spec)) if spec else
          f'<tr><td style="padding:8px 0;font-size:15px;color:{INK}">None.</td></tr>')

    pro = net.prospects()
    b += _section(f"High uninsured deposits, room under the cap ({len(pro)})",
                  f"Uninsured share {HIGH_UNINSURED:.0f}% or more, under half the cap used, not on the special cap: the most "
                  "run-exposed deposits and the most room for network coverage.")
    b += (table(pro[:ROWS], f'<th {THR}>UNINSURED $</th><th {TH}>LINKS</th>',
                lambda nb: f'<td {TDR}>{_fmt_money(nb.uninsured)}</td><td {TD} nowrap>{_links(nb.bank)}</td>')
          + more(min(ROWS, len(pro)), len(pro)) if pro else
          f'<tr><td style="padding:8px 0;font-size:15px;color:{INK}">None.</td></tr>')

    b += img("size", "By asset size")
    cov = (f"{s['banks']:,} banks from their latest Call Report ({net.quarter_label} where filed, else the prior quarter)"
           + (f"; {net.missing:,} not yet loaded (picked up next week)" if net.missing else "") + ".")
    b += (f'<tr><td style="padding:22px 0 0;font-size:13px;color:{MUTED}"><b style="color:{INK}">Coverage:</b> {_e(cov)} '
          f'<b style="color:{INK}">Attached:</b> four-page PDF and a spreadsheet with every bank. '
          f'<b style="color:{INK}">Sources:</b> FFIEC Call Reports (RC-E reciprocal deposits, RC-O uninsured deposits, RC total '
          f'liabilities); FDIC, Fed, OCC and FinCEN enforcement records.</td></tr>')
    return (f'<!doctype html><html><body style="margin:0;background:#eef0f3;font-family:-apple-system,Segoe UI,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f3"><tr><td align="center" style="padding:18px 10px">'
            f'<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;width:100%;background:#fff">'
            f'<tr><td style="background:{INK};padding:18px 24px"><div style="font-size:13px;font-weight:700;letter-spacing:.12em;color:#9aa3ad">WEEKLY</div>'
            f'<div style="font-size:23.6px;font-weight:700;color:#fff">{NAME}</div>'
            f'<div style="font-size:14.8px;color:#c8cdd3;margin-top:3px">{net.run_date:%A, %B %-d, %Y} · reciprocal cap use and uninsured deposits</div></td></tr>'
            f'<tr><td style="padding:18px 24px 26px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{b}</table></td></tr>'
            f'</table></td></tr></table></body></html>')


def subject(net: Network) -> str:
    s = net.stats()
    return (f"{NAME} · {net.run_date:%a %b %-d} · {s['near_cap'] + s['over_cap']} at {NEAR_CAP:.0f}%+ of cap · "
            f"{s['special']} likely on special cap · {s['high_uninsured']:,} with {HIGH_UNINSURED:.0f}%+ uninsured")


# ------------------------------------------------------------------ PDF (always four pages)
def render_pdf(net: Network) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, PageBreak, SimpleDocTemplate, Spacer, Table, TableStyle
    from . import pdf_report as P
    PAGES = 4
    s = net.stats()

    def header(title, n):
        t = Table([[P._p(escape(title), "h"), P._p(f"{n} / {PAGES}", "labr")]], colWidths=[P.WIDTH * 0.8, P.WIDTH * 0.2])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.5, P.INK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return [t, Spacer(1, 8)]

    def chart(cid, title, max_h):
        if cid not in net.images:
            return []
        im = Image(io.BytesIO(net.images[cid]))
        w = P.WIDTH
        h = w * im.imageHeight / im.imageWidth
        if h > max_h * inch:
            h = max_h * inch
            w = h * im.imageWidth / im.imageHeight
        im.drawWidth, im.drawHeight = w, h
        return [P._p(f"<b>{escape(title)}</b>", "cell"), Spacer(1, 3), im, Spacer(1, 8)]

    def tbl(rows, extra=None):
        head = [P._p("BANK", "label"), P._p("RECIPROCAL", "labr"), P._p("% OF CAP", "labr"), P._p("CHANGE", "labr"),
                P._p("UNINSURED", "labr")] + ([P._p(extra[0], "label")] if extra else [])
        body = [[P._bank(nb.bank), P._p(_fmt_money(nb.reciprocal), "num"), P._p(f"<b>{_pct(nb.used)}</b>", "num"),
                 P._p(_change(nb), "num"), P._p(_pct(nb.uninsured_share), "num")] + ([extra[1](nb)] if extra else [])
                for nb in rows]
        w = 0.95 * inch
        rest = P.WIDTH - 4 * w - (1.9 * inch if extra else 0)
        return P._table([head] + body, [rest] + [w] * 4 + ([1.9 * inch] if extra else []))

    def more(shown, total):
        return [Spacer(1, 3), P._p(f"Showing {shown} of {total}. All are in the attached spreadsheet.", "note")] if total > shown else []

    # page 1
    tiles = [("BANKS ANALYZED", f"{s['banks']:,}"), ("USE RECIPROCAL", f"{s['users']:,}"),
             ("RECIPROCAL TOTAL", _fmt_money(s["reciprocal_total"])), (f"AT {NEAR_CAP:.0f}%+ OF CAP", str(s["near_cap"] + s["over_cap"])),
             ("LIKELY SPECIAL CAP", str(s["special"])), (f"UNINSURED {HIGH_UNINSURED:.0f}%+", f"{s['high_uninsured']:,}")]
    tw = (P.WIDTH - 5 * 6) / 6
    cells = []
    for lab, val in tiles:
        t = Table([[P._p(val, "tile")], [P._p(lab, "label")]], colWidths=[tw])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, P.RULE), ("LINEABOVE", (0, 0), (-1, 0), 3.5, colors.HexColor("#2a78d6")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (0, 0), 6),
                               ("BOTTOMPADDING", (0, -1), (-1, -1), 6)]))
        cells += [t, ""]
    row = Table([cells[:-1]], colWidths=[tw, 6] * 5 + [tw])
    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    p1 = header("Summary", 1) + [row, Spacer(1, 10)]
    if net.summary:
        box = Table([[[P._p("ANALYSIS", "label"), Spacer(1, 3), P._p(escape(net.summary), "body")]]], colWidths=[P.WIDTH])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), P.SOFT), ("LINEBEFORE", (0, 0), (0, -1), 2.5, P.INK),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                 ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
        p1 += [box, Spacer(1, 10)]
    p1 += chart("usage", "How much of the reciprocal cap banks use", 2.6)
    p1 += [P._p(escape(CAP_NOTE), "small")]

    # page 2
    p2 = header("Exposure map and size", 2) + chart("map", "Uninsured deposits vs. reciprocal cap used (bubble = assets; red = likely special cap)", 4.3)
    p2 += chart("size", "By asset size", 2.6)

    # page 3
    near, spec = net.near_cap(), net.special()

    def page3(cap):
        out = header("Cap pressure", 3) + [P._p(f"<b>NEAREST OR OVER THE CAP ({len(near)})</b>", "label"), Spacer(1, 3)]
        out += ([tbl(near[:cap])] + more(min(cap, len(near)), len(near)) if near else [P._p("None.", "cell")])
        out += [Spacer(1, 10), P._p(f"<b>LIKELY ON THE SPECIAL CAP ({len(spec)})</b>: held to the four-quarter average", "label"),
                Spacer(1, 3)]
        out += ([tbl(spec[:cap], ("WHY", lambda nb: P._p(escape("; ".join(nb.special_cap)), "small")))]
                + more(min(cap, len(spec)), len(spec)) if spec else [P._p("None.", "cell")])
        return out

    # page 4
    pro = net.prospects()

    def page4(cap):
        out = header("High uninsured deposits, room under the cap", 4)
        out += [P._p(f"Uninsured share {HIGH_UNINSURED:.0f}% or more, under half the cap used, not on the special cap "
                     f"({len(pro)} banks), largest uninsured balance first.", "note"), Spacer(1, 6)]
        out += ([tbl(pro[:cap], ("UNINSURED $", lambda nb: P._p(_fmt_money(nb.uninsured), "num")))]
                + more(min(cap, len(pro)), len(pro)) if pro else [P._p("None.", "cell")])
        out += [Spacer(1, 12), P._p("<b>Sources:</b> FFIEC Call Reports (RC-E memorandum reciprocal deposits, RC-O memorandum "
                                    "estimated uninsured deposits, RC total liabilities); FDIC, Fed, OCC and FinCEN enforcement "
                                    "records. Coverage: " + f"{s['banks']:,} banks"
                                    + (f"; {net.missing:,} not yet loaded" if net.missing else "") + ".", "small")]
        return out

    pages = [P._fit(lambda _c: p1, 0), P._fit(lambda _c: p2, 0), P._fit(page3, 18), P._fit(page4, 30)]
    story = []
    for i, pg in enumerate(pages):
        story += pg + ([PageBreak()] if i < len(pages) - 1 else [])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=P.MARGIN, rightMargin=P.MARGIN, topMargin=P.TOP,
                            bottomMargin=P.BOTTOM, title=f"{NAME} {net.run_date:%Y-%m-%d}", author=NAME)

    def chrome(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(P.INK)
        canvas.rect(0, P.PAGE_H - 0.75 * inch, P.PAGE_W, 0.75 * inch, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 15)
        canvas.drawString(P.MARGIN, P.PAGE_H - 0.42 * inch, NAME)
        canvas.setFont("Helvetica", 10)
        canvas.setFillColor(colors.HexColor("#c8cdd3"))
        canvas.drawString(P.MARGIN, P.PAGE_H - 0.6 * inch, f"{net.run_date:%A, %B %-d, %Y}  ·  reciprocal cap use and uninsured deposits")
        canvas.setFillColor(P.MUTED)
        canvas.setFont("Helvetica", 8.5)
        canvas.drawString(P.MARGIN, 0.35 * inch, f"{NAME} · {net.run_date:%B %-d, %Y}")
        canvas.drawRightString(P.PAGE_W - P.MARGIN, 0.35 * inch, f"{doc_.page} / {PAGES}")
        canvas.restoreState()

    doc.build(story, onFirstPage=chrome, onLaterPages=chrome)
    return buf.getvalue()


# ------------------------------------------------------------------ spreadsheet
def build_workbook(net: Network) -> bytes:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule, FormulaRule
    from openpyxl.styles import Alignment, Border
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from .workbook import AMBER_FILL, INPUT_FILL, RED_FILL, SIZE_BUCKETS, THIN, _font, _header_row, _link, _title, _widths
    wb = Workbook()
    bs = wb.active
    bs.title = "Banks"
    _title(bs, f"{NAME}: every bank", f"{net.run_date:%B %-d, %Y}. Dollar figures in $000. Cap = lesser of $5B or 20% of liabilities.")
    heads = ["Institution", "City", "State", "RSSD", "FDIC cert", "Period", "Total assets", "Total deposits",
             "Reciprocal deposits", "Reciprocal cap", "% of cap used", "Headroom", "Uninsured deposits", "Uninsured share",
             "Likely special cap", "Last quarter % of cap", "Call Report", "UBPR", "FDIC profile"]
    hr = 4
    _header_row(bs, hr, heads)
    r = hr
    for nb in sorted(net.banks, key=lambda x: -(x.assets or 0)):
        r += 1
        b = nb.bank
        vals = [b.name, b.location.rsplit(",", 1)[0], b.location.rsplit(",", 1)[-1].strip(), b.rssd, b.cert or None,
                f"Q{(nb.period.month - 1) // 3 + 1} {nb.period.year}", nb.assets, nb.deposits, nb.reciprocal, nb.cap,
                f"=IF(AND(ISNUMBER(I{r}),ISNUMBER(J{r}),J{r}>0),I{r}/J{r},\"\")", f"=IF(AND(ISNUMBER(I{r}),ISNUMBER(J{r})),J{r}-I{r},\"\")",
                nb.uninsured, f"=IF(AND(ISNUMBER(M{r}),ISNUMBER(H{r}),H{r}>0),M{r}/H{r},\"\")",
                "; ".join(nb.special_cap), None if nb.prior_used is None else nb.prior_used / 100]
        for c, v in enumerate(vals, 1):
            cell = bs.cell(row=r, column=c, value=v)
            cell.font = _font()
            cell.border = Border(bottom=THIN)
        for c in (7, 8, 9, 10, 12, 13):
            bs.cell(row=r, column=c).number_format = "#,##0"
        for c in (11, 14, 16):
            bs.cell(row=r, column=c).number_format = "0.0%"
        _link(bs.cell(row=r, column=17), b.call_report_url, "Call Report")
        _link(bs.cell(row=r, column=18), b.ubpr_url, "UBPR")
        _link(bs.cell(row=r, column=19), b.bankfind_url, "FDIC profile")
    last = max(r, hr + 1)
    bs.conditional_formatting.add(f"K{hr + 1}:K{last}", CellIsRule(operator="greaterThan", formula=["1"], fill=RED_FILL))
    bs.conditional_formatting.add(f"K{hr + 1}:K{last}", CellIsRule(operator="between", formula=[str(NEAR_CAP / 100), "1"], fill=AMBER_FILL))
    bs.conditional_formatting.add(f"N{hr + 1}:N{last}", CellIsRule(operator="greaterThanOrEqual", formula=[str(HIGH_UNINSURED / 100)], fill=AMBER_FILL))
    bs.conditional_formatting.add(f"O{hr + 1}:O{last}", FormulaRule(formula=[f"LEN(O{hr + 1})>0"], fill=RED_FILL))
    if r > hr:
        t = Table(displayName="Banks", ref=f"A{hr}:{get_column_letter(len(heads))}{last}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        bs.add_table(t)
    bs.freeze_panes = f"B{hr + 1}"
    _widths(bs, {"A": 34, "B": 16, "C": 6, "D": 10, "E": 9, "F": 9, "G": 13, "H": 13, "I": 13, "J": 13, "K": 10, "L": 13,
                 "M": 13, "N": 10, "O": 26, "P": 11, "Q": 12, "R": 8, "S": 12})

    ss = wb.create_sheet("Summary", 0)
    _title(ss, f"{NAME}: summary", "Live formulas over the Banks sheet. Yellow cells are editable thresholds.")
    rng = lambda c: f"Banks!${c}${hr + 1}:${c}${last}"          # noqa: E731
    ss["A4"], ss["B4"] = "Near-cap threshold (% of cap)", NEAR_CAP / 100
    ss["A5"], ss["B5"] = "High uninsured threshold (% of deposits)", HIGH_UNINSURED / 100
    for c in ("B4", "B5"):
        ss[c].fill = INPUT_FILL
        ss[c].font = _font(color="0000FF")
        ss[c].number_format = "0%"
    rows = [("Banks analyzed", f"=COUNTA({rng('A')})", "#,##0"),
            ("Banks holding reciprocal deposits", f'=COUNTIF({rng("I")},">0")', "#,##0"),
            ("Share of banks using reciprocal", "=IF(B7=0,\"\",B8/B7)", "0.0%"),
            ("Total reciprocal deposits ($000)", f"=SUM({rng('I')})", "#,##0"),
            ("Median % of cap used (users)", "ARRAY", "0.0%"),
            ("Banks at or above the near-cap threshold", f'=COUNTIF({rng("K")},">="&B4)', "#,##0"),
            ("Banks over the cap", f'=COUNTIF({rng("K")},">1")', "#,##0"),
            ("Reciprocal users likely on the special cap", f'=COUNTIFS({rng("O")},"?*",{rng("I")},">0")', "#,##0"),
            ("Total uninsured deposits ($000)", f"=SUM({rng('M')})", "#,##0"),
            ("Median uninsured share", f"=IFERROR(MEDIAN({rng('N')}),\"\")", "0.0%"),
            ("Banks at or above the high-uninsured threshold", f'=COUNTIF({rng("N")},">="&B5)', "#,##0")]
    for i, (k, f, fmt) in enumerate(rows, 7):
        ss.cell(row=i, column=1, value=k).font = _font()
        if f == "ARRAY":
            from openpyxl.worksheet.formula import ArrayFormula
            f = ArrayFormula(f"B{i}", f'=IFERROR(MEDIAN(IF({rng("I")}>0,{rng("K")})),"")')
        c = ss.cell(row=i, column=2, value=f)
        c.font = _font(bold=True)
        c.number_format = fmt
    top = 7 + len(rows) + 2
    ss.cell(row=top, column=1, value="By asset size ($000)").font = _font(bold=True, size=12)
    _header_row(ss, top + 1, ["Asset size", "From", "To", "Banks", "Using reciprocal", "% using", "Reciprocal ($000)",
                              "Uninsured ($000)", "Uninsured / deposits"])
    for i, (name, lo, hi) in enumerate(SIZE_BUCKETS):
        rr = top + 2 + i
        A = rng("G")
        vals = [name, lo, hi, f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr})', f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr},{rng("I")},">0")',
                f"=IF(D{rr}=0,\"\",E{rr}/D{rr})", f'=SUMIFS({rng("I")},{A},">="&B{rr},{A},"<"&C{rr})',
                f'=SUMIFS({rng("M")},{A},">="&B{rr},{A},"<"&C{rr})',
                f'=IFERROR(H{rr}/SUMIFS({rng("H")},{A},">="&B{rr},{A},"<"&C{rr}),"")']
        for c, v in enumerate(vals, 1):
            cell = ss.cell(row=rr, column=c, value=v)
            cell.font = _font(bold=(c == 1))
            cell.number_format = "0.0%" if c in (6, 9) else "#,##0"
    _widths(ss, {"A": 46, "B": 16, "C": 14, "D": 10, "E": 15, "F": 10, "G": 17, "H": 17, "I": 18})

    for title, lst in (("Near cap", net.near_cap()), ("Special cap", net.special()), ("High uninsured", net.prospects())):
        ws = wb.create_sheet(title)
        _title(ws, title, CAP_NOTE if title != "High uninsured" else
               f"Uninsured {HIGH_UNINSURED:.0f}%+ of deposits, under half the cap used, not on the special cap.")
        _header_row(ws, 4, ["Institution", "Location", "Total assets", "Reciprocal ($000)", "% of cap", "Last quarter % of cap",
                            "Uninsured ($000)", "Uninsured share", "Likely special cap", "Call Report", "UBPR"])
        for i, nb in enumerate(lst, 5):
            vals = [nb.bank.name, nb.bank.location, nb.bank.assets, nb.reciprocal, None if nb.used is None else nb.used / 100,
                    None if nb.prior_used is None else nb.prior_used / 100, nb.uninsured,
                    None if nb.uninsured_share is None else nb.uninsured_share / 100, "; ".join(nb.special_cap)]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(row=i, column=c, value=v)
                cell.font = _font(bold=(c == 1))
                cell.number_format = "0.0%" if c in (5, 6, 8) else "#,##0" if c in (4, 7) else "General"
            _link(ws.cell(row=i, column=10), nb.bank.call_report_url, "Call Report")
            _link(ws.cell(row=i, column=11), nb.bank.ubpr_url, "UBPR")
        if not lst:
            ws.cell(row=5, column=1, value="None.").font = _font(italic=True)
        _widths(ws, {"A": 34, "B": 22, "C": 12, "D": 16, "E": 10, "F": 14, "G": 16, "H": 12, "I": 30, "J": 12, "K": 8})
        ws.freeze_panes = "B5"
    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
        for row in ws.iter_rows(min_row=1, max_row=2):
            for c in row:
                c.alignment = Alignment(wrap_text=False)
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
