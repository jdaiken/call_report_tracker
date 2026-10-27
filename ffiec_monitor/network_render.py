"""Email (highlights), ten-page PDF and spreadsheet for the weekly Network Exposure report (network.py)."""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from .network import (GROUP_NOTES, RISK_MEASURES, risk_score, EXCLUDE_ASSETS, HIGH_UNINSURED, NAME, NEAR_CAP, QUARTERS, TOP_N, Network, NetBank, _fmt_money, _pct,
                      short, takeaways)

PAGES = 10
EMAIL_IMAGES = ("risk", "groups", "trend_cap", "special", "trend_special")
RISK_NOTE = ("Every reciprocal user scored on five measures, 0 (fine) to 3 (high) each: cap used (60/80/100%), likely special "
             "cap (3; over its estimate shown), capital cushion above well capitalized (under 1/0.5/0 pts), uninsured share "
             "(40/50/60%), and the change over the past year in the gap to the cap or the special cap.")
CAP_NOTE = ("Reciprocal deposits are excepted from brokered only up to the lesser of $5 billion or 20% of total liabilities "
            "(12 CFR 337.6(e)). Banks not well capitalized and well rated are held to a special cap, their four-quarter "
            "average; ratings aren't public, so banks below well capitalized or under an enforcement order are shown as "
            "likely on it.")
EXCLUDE_NOTE = (f"Banks with ${EXCLUDE_ASSETS // 1_000_000} billion or more in assets are left out: every U.S. GSIB (JPMorgan "
                "Chase, Bank of America, Citibank, Wells Fargo, Goldman Sachs, Morgan Stanley, BNY Mellon, State Street), "
                "PNC, U.S. Bank, Truist, Capital One, and large regionals and brokerage-affiliated banks such as Raymond James Bank.")


def _qlab(d) -> str:
    return f"Q{(d.month - 1) // 3 + 1} {str(d.year)[2:]}"


def _quarters(net: Network) -> list:
    """Quarter ends for the trend tables, oldest first (from the first bank with full history)."""
    for b in net.banks:
        if len(b.history) >= QUARTERS:
            return [h[0] for h in reversed(b.history[:QUARTERS])]
    return []


def _at(nb: NetBank, q):
    for h in nb.history:
        if h[0] == q:
            return h
    return None


def _over(nb: NetBank) -> str:
    o = nb.over_special
    if o is None:
        return ""
    return _fmt_money(o) if o > 0 else "within"


def _change(nb: NetBank) -> str:
    if nb.prior_used is None or nb.used is None:
        return ""
    d = nb.used - nb.prior_used
    return f"{'+' if d >= 0 else '-'}{abs(d):.1f} pts"


def _tiles(net: Network) -> list[tuple[str, str]]:
    s = net.stats()
    return [("BANKS ANALYZED", f"{s['banks']:,}"), ("USE RECIPROCAL", f"{s['users']:,}"),
            ("RECIPROCAL TOTAL", _fmt_money(s["reciprocal_total"])),
            (f"AT {NEAR_CAP:.0f}%+ OF CAP", f"{s['near_cap'] + s['over_cap']}"),
            ("LIKELY SPECIAL CAP", f"{s['special_all']}"), (f"UNINSURED {HIGH_UNINSURED:.0f}%+", f"{s['high_uninsured']:,}")]


# ------------------------------------------------------------------ email: highlights only
def render_html(net: Network) -> str:
    from .report import AMBER, GREEN, INK, MUTED, RED, RULE, SOFT, _e, _tile
    s = net.stats()
    colors = ["#4b5563", "#2a78d6", "#2a78d6", AMBER if s["near_cap"] + s["over_cap"] else GREEN,
              RED if s["special_all"] else GREEN, "#eb6834"]
    tiles = [(lab, val, col) for (lab, val), col in zip(_tiles(net), colors)]
    b = ('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
         + '<td width="8"></td>'.join(_tile(lab, val, col) for lab, val, col in tiles) + "</tr></table></td></tr>")
    if net.summary:
        b += (f'<tr><td style="padding:16px 0 0"><div style="background:{SOFT};border-left:3px solid {INK};padding:10px 12px">'
              f'<div style="font-size:12.4px;font-weight:700;letter-spacing:.08em;color:{MUTED}">ANALYSIS</div>'
              f'<div style="font-size:15.9px;line-height:1.5;color:{INK};margin-top:4px">{_e(net.summary)}</div></div></td></tr>')

    def h(title, note=""):
        n = f'<div style="font-size:13.5px;color:{MUTED};margin-top:2px">{_e(note)}</div>' if note else ""
        return (f'<tr><td style="padding:22px 0 6px;border-bottom:2px solid {INK}"><div style="font-size:18px;'
                f'font-weight:700;color:{INK}">{_e(title)}</div>{n}</td></tr>')

    if "risk" in net.images:
        b += h("Highest-risk banks this week", RISK_NOTE)
        b += (f'<tr><td style="padding:8px 0 0"><img src="cid:net_risk" width="712" alt="Highest-risk banks" '
              f'style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')
    if "groups" in net.images:
        b += h("Three groups to watch", "Each panel is a different kind of risk. Red “▲ rising” = getting worse over the past year.")
        b += (f'<tr><td style="padding:8px 0 0"><img src="cid:net_groups" width="712" alt="Three groups to watch" '
              f'style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')
        b += "".join(f'<tr><td style="padding:8px 0 0;font-size:13.5px;line-height:1.45;color:{INK}"><b>{_e(t)}.</b> '
                     f'<span style="color:{MUTED}">{_e(n)}</span></td></tr>' for t, n in GROUP_NOTES)

    def mini(title, note, lst, metric):
        if not lst:
            return ""
        rows = "".join(
            f'<tr><td style="padding:5px 0;font-size:14.5px;color:{INK};border-bottom:1px solid {RULE}"><b>{_e(short(nb.bank.name, 40))}</b>'
            f' <span style="font-size:12.5px;color:{MUTED}">{_e(nb.bank.location)}</span></td>'
            f'<td style="padding:5px 0;font-size:14.5px;color:{INK};border-bottom:1px solid {RULE};text-align:right;white-space:nowrap">'
            f'{metric(nb)}</td></tr>' for nb in lst[:5])
        return h(title, note) + f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>'

    def img(cid, alt):
        return (f'<tr><td style="padding:8px 0 0"><img src="cid:net_{cid}" width="712" alt="{_e(alt)}" '
                f'style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>' if cid in net.images else "")

    near, spec, close = net.near_cap(), net.special(), net.closest_to_special()
    b += mini(f"Nearest or over the cap ({len(near)})", "More placements above 100% count as brokered.", near,
              lambda nb: f"<b>{_pct(nb.used)}</b> of cap · {_fmt_money(nb.reciprocal)}")
    if "trend_cap" in net.images:
        tr = net.trend()
        wid = sum(1 for x in tr if x.gap_change() > 0)
        b += h("Is the gap getting worse? Last five quarters",
               f"Reciprocal deposits (line) against the cap (dashed); red shading = over the cap. The gap widened at {wid} of "
               f"{len(tr)} banks at {NEAR_CAP:.0f}%+ of cap over the past year.")
        b += img("trend_cap", "Five-quarter trend against the cap")
    b += mini(f"Likely on the special cap ({len(spec)})", "Enforcement order or below well capitalized; held to the "
              "four-quarter average (estimated).", spec,
              lambda nb: f"{_fmt_money(nb.reciprocal)} vs. est. {_fmt_money(nb.special_cap_est)}")
    b += img("special", "Reciprocal deposits vs. estimated special cap")
    if "trend_special" in net.images:
        b += h("Special-cap banks: last five quarters", "Reciprocal deposits against the estimated special cap (dashed).")
        b += img("trend_special", "Special-cap banks over five quarters")
    b += mini("Closest to the special cap", "Thinnest capital cushion above a well-capitalized line.", close,
              lambda nb: f"<b>{nb.cushion:+.2f} pts</b> · {_e(nb.binding)}")

    cov = (f"{s['banks']:,} banks, latest Call Report ({net.quarter_label} where filed, else the prior quarter)"
           + (f"; {len(net.excluded)} banks with ${EXCLUDE_ASSETS // 1_000_000}B+ in assets left out" if net.excluded else "")
           + (f"; {net.missing:,} not yet loaded" if net.missing else "") + ".")
    b += (f'<tr><td style="padding:22px 0 0;font-size:14px;color:{INK}"><b>Full report attached:</b> ten-page PDF with '
          f'charts for every section, and a spreadsheet with every bank.</td></tr>'
          f'<tr><td style="padding:8px 0 0;font-size:12.5px;color:{MUTED}">{_e(cov)} Sources: FFIEC Call Reports; FDIC, '
          f'Fed, OCC and FinCEN enforcement records.</td></tr>')
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
            f"{s['special_all']} likely on special cap · {s['high_uninsured']:,} with {HIGH_UNINSURED:.0f}%+ uninsured")


# ------------------------------------------------------------------ PDF (always ten pages)
def render_pdf(net: Network) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, PageBreak, SimpleDocTemplate, Spacer, Table, TableStyle
    from . import pdf_report as P
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
        return ([P._p(f"<b>{escape(title)}</b>", "cell"), Spacer(1, 3)] if title else []) + [im, Spacer(1, 8)]

    def note(text):
        return [P._p(escape(text), "note"), Spacer(1, 6)]

    def none(text="None this week."):
        return [P._p(text, "cell")]

    def more(shown, total):
        return [Spacer(1, 3), P._p(f"Showing {shown} of {total}. All are in the attached spreadsheet.", "note")] if total > shown else []

    def tbl(rows, cols):
        """cols: [(label, width_in or None, fn(nb) -> Paragraph)]; the None width takes the rest."""
        fixed = sum(w for _, w, _ in cols if w)
        widths = [(w or 0) * inch or (P.WIDTH - fixed * inch) for _, w, _ in cols]
        head = [P._p(lab, "label" if i == 0 else "labr") for i, (lab, _, _) in enumerate(cols)]
        return P._table([head] + [[fn(nb) for _, _, fn in cols] for nb in rows], widths)

    num = lambda t: P._p(t, "num")                                  # noqa: E731
    BANK = ("BANK", None, lambda nb: P._bank(nb.bank))
    REC = ("RECIPROCAL", 1.0, lambda nb: num(_fmt_money(nb.reciprocal)))
    CAP = ("CAP", 0.9, lambda nb: num(_fmt_money(nb.cap)))
    USED = ("% OF CAP", 0.85, lambda nb: num(f"<b>{_pct(nb.used)}</b>"))
    CHG = ("VS. LAST Q", 0.85, lambda nb: num(_change(nb)))
    UNI = ("UNINSURED", 0.85, lambda nb: num(_pct(nb.uninsured_share)))
    EST = ("EST. SPECIAL CAP", 1.15, lambda nb: num(_fmt_money(nb.special_cap_est)))
    ABOVE = ("ABOVE IT", 0.85, lambda nb: num(_over(nb)))

    # 1. Summary
    tw = (P.WIDTH - 5 * 6) / 6
    cells = []
    for lab, val in _tiles(net):
        t = Table([[P._p(val if len(val) <= 6 else f"<font size='17'>{val}</font>", "tile")], [P._p(lab, "label")]],
                  colWidths=[tw])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, P.RULE), ("LINEABOVE", (0, 0), (-1, 0), 3.5, colors.HexColor("#2a78d6")),
                               ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (0, 0), 6),
                               ("BOTTOMPADDING", (0, -1), (-1, -1), 6)]))
        cells += [t, ""]
    row = Table([cells[:-1]], colWidths=[tw, 6] * 5 + [tw])
    row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    tk = takeaways(net)

    def p1(cap):
        """cap counts down: footnotes go first, then the lowest takeaways, until the page fits."""
        out = header("Summary", 1) + [row, Spacer(1, 12)]
        if net.summary:
            box = Table([[[P._p("ANALYSIS", "label"), Spacer(1, 3), P._p(escape(net.summary), "body")]]], colWidths=[P.WIDTH])
            box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), P.SOFT), ("LINEBEFORE", (0, 0), (0, -1), 2.5, P.INK),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                     ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            out += [box, Spacer(1, 12)]
        out += [P._p("KEY TAKEAWAYS", "label"), Spacer(1, 4)]
        for t in tk[:max(1, min(cap, len(tk)))]:
            out += [P._p(f"•&nbsp;&nbsp;{escape(t)}", "body"), Spacer(1, 3)]
        out += [Spacer(1, 10), P._p("IN THIS REPORT", "label"), Spacer(1, 3),
                P._p("2 Highest-risk banks · 3 Three groups to watch · 4 Top 20 holders · 5 Nearest the cap · 6 Five-quarter trend vs. "
                     "the cap · 7 Special cap · 8 Special-cap trend · 9 Closest to the special cap · 10 All banks by size, and notes",
                     "note")]
        if cap > len(tk):
            out += [Spacer(1, 10), P._p(escape(CAP_NOTE), "small"), Spacer(1, 4), P._p(escape(EXCLUDE_NOTE), "small")]
        return out

    # 2. Highest-risk banks
    rk = net.risk()

    def p2(cap):
        out = header("Highest-risk banks this week", 2) + note(RISK_NOTE)
        out += chart("risk", "", 7.6)
        return out + ([] if rk else none()) + note("Locations, links and every bank's measures are in the Banks sheet of the attached spreadsheet.")

    # 3. Three groups to watch
    top = net.top_exposure()

    def p3(_cap):
        out = header("Three groups to watch", 3)
        out += note("Each panel is a different kind of risk to the network. Red \"rising\" = getting worse over the past year.")
        out += chart("groups", "", 7.7)
        for t, n in GROUP_NOTES:
            out += [P._p(f"<b>{escape(t)}.</b> {escape(n)}", "small"), Spacer(1, 4)]
        return out

    # 4. Top 20 holders
    def p4(cap):
        out = header(f"Top {len(top)} reciprocal holders against their caps", 4)
        out += chart("top", "Reciprocal deposits (bar) vs. cap (grey) and estimated special cap (tick)", 4.4)
        return out + ([tbl(top[:cap], [BANK, REC, CAP, USED, CHG, EST])] if top else none())

    # 5. Nearest or over the cap
    near = net.near_cap()

    def p5(cap):
        out = header(f"Nearest or over the reciprocal cap ({len(near)})", 5)
        out += note(f"Banks at {NEAR_CAP:.0f}% of the cap or more. Above 100%, further reciprocal placements count as brokered.")
        out += chart("nearcap", f"Share of the cap used, highest {min(15, len(near))}", 3.8)
        return out + ([tbl(near[:cap], [BANK, REC, USED, CHG, UNI])] + more(min(cap, len(near)), len(near)) if near else none())

    # 6. Special cap
    spec = net.special()

    def p7(cap):
        out = header(f"Likely on the special cap ({len(spec)})", 7)
        out += note("Every bank below well capitalized or under an active FDIC, Fed, OCC or FinCEN order, whether or not it holds "
                    "reciprocal deposits now. Its reciprocal deposits are limited to the four-quarter average (estimated from the "
                    "four quarter-ends before this one); anything above counts as brokered.")
        out += chart("special", "Banks holding reciprocal deposits: now vs. estimated special cap", 3.3)
        why = ("WHY", 1.45, lambda nb: P._p(escape("; ".join(nb.special_cap)), "small"))
        estb = ("EST. SPECIAL CAP", 1.15, lambda nb: num(_fmt_money(nb.special_cap_est)
                                                         + (f"<br/><font size='7' color='#5b626c'>{nb.est_basis}</font>" if nb.est_basis else "")))
        return out + ([tbl(spec[:cap], [BANK, REC, estb, ABOVE, why])] + more(min(cap, len(spec)), len(spec)) if spec else none())

    # 7. Closest to the special cap
    close = net.closest_to_special()

    def p9(cap):
        out = header("Closest to the special cap", 9)
        out += note("Reciprocal users not on the special cap, thinnest capital cushion first (community bank leverage ratio banks "
                    "against 9%). If one slips below well capitalized, it is held to its estimated special cap.")
        out += chart("cushion", "Capital cushion above the nearest well-capitalized line", 3.7)
        cush = ("CUSHION", 1.4, lambda nb: P._p(f"<b>{nb.cushion:+.2f} pts</b><br/><font size='7' color='#5b626c'>"
                                                f"{escape(nb.binding)}</font>", "num"))
        return out + ([tbl(close[:cap], [BANK, cush, REC, EST, ABOVE])] if close else none())

    # 8. Uninsured
    pro = net.prospects()

    def p10(cap):
        out = header("Across all banks, and notes", 10)
        out += chart("size", "Share of the reciprocal cap used, by bank size", 2.9)
        out += chart("uninsured", "Uninsured share of deposits, by bank size", 2.4)
        out += [P._p("<b>NOTES</b>", "label"), Spacer(1, 3)]
        for t in (CAP_NOTE, EXCLUDE_NOTE,
                  "Estimated special cap: average of the four quarter-end reciprocal balances before the current quarter "
                  "(fewer when a bank hasn't filed that long; 'nQ avg' shows how many). Shown when at least two quarters are on hand.",
                  "Exam ratings aren't public, so 'likely on the special cap' means below well capitalized or under an active "
                  "FDIC, Fed, OCC or FinCEN order (civil money penalties and orders over 10 years old excluded).",
                  f"Coverage: {s['banks']:,} banks from their latest Call Report ({net.quarter_label} where filed, else the prior "
                  f"quarter)" + (f"; {len(net.excluded)} large banks left out" if net.excluded else "")
                  + (f"; {net.missing:,} not yet loaded" if net.missing else "") + "."):
            out += [P._p(f"•&nbsp;&nbsp;{escape(t)}", "small"), Spacer(1, 3)]
        out += [Spacer(1, 6), P._p("<b>Sources:</b> FFIEC Call Reports (RC-E memorandum reciprocal deposits, RC-O memorandum "
                                   "estimated uninsured deposits, RC total liabilities, RC-R capital ratios); FDIC ED&amp;O, "
                                   "Federal Reserve, OCC and FinCEN enforcement records. UBPR and Call Report links for every bank "
                                   "are in the spreadsheet.", "small")]
        return out

    qs = _quarters(net)

    def qcols(fn):
        return [(_qlab(q), 0.68, (lambda q: lambda nb: num(fn(nb, q)))(q)) for q in qs]

    def pct_at(nb, q):
        h = _at(nb, q)
        return _pct(h[1] / h[2] * 100) if h and h[1] is not None and h[2] else "—"

    def rec_at(nb, q):
        h = _at(nb, q)
        return _fmt_money(h[1]) if h and h[1] is not None else "—"

    tr = net.trend()

    def p6(cap):
        wid = sum(1 for x in tr if x.gap_change() > 0)
        out = header("Is the gap getting worse? Five quarters against the cap", 6)
        out += note(f"Banks at {NEAR_CAP:.0f}%+ of the cap, largest widening first. Line = reciprocal deposits; dashed = the cap "
                    f"(it moves with total liabilities); red shading = over the cap. Over the past year the gap widened at {wid} "
                    f"of {len(tr)} banks.")
        out += chart("trend_cap", "", 4.2)
        chg = ("GAP CHANGE", 0.95, lambda nb: num(f"<b>{'+' if nb.gap_change() >= 0 else '-'}{_fmt_money(abs(nb.gap_change()))}</b>"
                                                 f"<br/><font size='7' color='#5b626c'>{'wider' if nb.gap_change() > 0 else 'narrower'}</font>"))
        return out + ([tbl(tr[:cap], [BANK] + qcols(pct_at) + [chg])] + more(min(cap, len(tr)), len(tr)) if tr else
                      none("Not enough history on hand yet."))

    sp = [x for x in net.special_users() if len(x.series()) >= 2]

    def p8(cap):
        out = header("Special-cap banks: five quarters", 8)
        out += note("Reciprocal deposits each quarter against the estimated special cap (dashed). Rising balances while held "
                    "to the special cap mean more would count as brokered.")
        out += chart("trend_special", "", 4.2)
        dirn = ("1-YR CHANGE", 0.95, lambda nb: num(f"<b>{'+' if nb.special_gap_change() >= 0 else '-'}"
                                                   f"{_fmt_money(abs(nb.special_gap_change()))}</b>"))
        return out + ([tbl(sp[:cap], [BANK] + qcols(rec_at) + [EST, dirn])] + more(min(cap, len(sp)), len(sp)) if sp else
                      none("Not enough history on hand yet."))

    pages = [P._fit(p1, len(tk) + 1), P._fit(p2, 8), P._fit(p3, 0), P._fit(p4, TOP_N), P._fit(p5, 12),
             P._fit(p6, 12), P._fit(p7, 12), P._fit(p8, 12), P._fit(p9, 12), P._fit(p10, 0)]
    def squeeze(page):
        """Shrink the page's charts until it fits one page, so the PDF is always exactly PAGES long."""
        for _ in range(12):
            over = P._height(page) - P.FRAME_H
            if over <= 0:
                break
            ims = [f for f in page if isinstance(f, Image)]
            if not ims:
                break
            total = sum(f.drawHeight for f in ims)
            k = max(0.5, (total - over - 4) / total)
            for f in ims:
                f.drawHeight *= k
                f.drawWidth *= k
        return page

    pages = [squeeze(pg) for pg in pages]
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
             "Likely special cap", "Last quarter % of cap", "Call Report", "UBPR", "FDIC profile",
             "Capital cushion (pts)", "Nearest line", "Est. special cap (4Q avg)", "Above est. special cap", "Risk score (0-15)"]
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
        for c, v in ((20, nb.cushion), (21, nb.binding), (22, nb.special_cap_est)):
            cell = bs.cell(row=r, column=c, value=v)
            cell.font = _font()
        bs.cell(row=r, column=20).number_format = "+0.00;-0.00"
        bs.cell(row=r, column=22).number_format = "#,##0"
        bs.cell(row=r, column=23, value=f'=IF(AND(ISNUMBER(V{r}),ISNUMBER(I{r})),I{r}-V{r},"")').number_format = "#,##0;(#,##0);-"
        bs.cell(row=r, column=23).font = _font()
        bs.cell(row=r, column=24, value=risk_score(nb)[0] if (nb.reciprocal or 0) > 0 else None).font = _font(bold=True)
    last = max(r, hr + 1)
    bs.conditional_formatting.add(f"K{hr + 1}:K{last}", CellIsRule(operator="greaterThan", formula=["1"], fill=RED_FILL))
    bs.conditional_formatting.add(f"K{hr + 1}:K{last}", CellIsRule(operator="between", formula=[str(NEAR_CAP / 100), "1"], fill=AMBER_FILL))
    bs.conditional_formatting.add(f"N{hr + 1}:N{last}", CellIsRule(operator="greaterThanOrEqual", formula=[str(HIGH_UNINSURED / 100)], fill=AMBER_FILL))
    bs.conditional_formatting.add(f"O{hr + 1}:O{last}", FormulaRule(formula=[f"LEN(O{hr + 1})>0"], fill=RED_FILL))
    bs.conditional_formatting.add(f"T{hr + 1}:T{last}", CellIsRule(operator="lessThan", formula=["0.5"], fill=AMBER_FILL))
    bs.conditional_formatting.add(f"W{hr + 1}:W{last}", CellIsRule(operator="greaterThan", formula=["0"], fill=RED_FILL))
    if r > hr:
        t = Table(displayName="Banks", ref=f"A{hr}:{get_column_letter(len(heads))}{last}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        bs.add_table(t)
    bs.freeze_panes = f"B{hr + 1}"
    _widths(bs, {"A": 34, "B": 16, "C": 6, "D": 10, "E": 9, "F": 9, "G": 13, "H": 13, "I": 13, "J": 13, "K": 10, "L": 13,
                 "M": 13, "N": 10, "O": 26, "P": 11, "Q": 12, "R": 8, "S": 12, "T": 11, "U": 22, "V": 15, "W": 15, "X": 12})

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
    ex = top + 2 + len(SIZE_BUCKETS) + 1
    ss.cell(row=ex, column=1, value=EXCLUDE_NOTE + (f" Left out this week: {len(net.excluded)}." if net.excluded else "")
            ).font = _font(color="5B626C", italic=True)
    _widths(ss, {"A": 46, "B": 16, "C": 14, "D": 10, "E": 15, "F": 10, "G": 17, "H": 17, "I": 18})

    qs = _quarters(net)
    ts = wb.create_sheet("5-quarter trend")
    _title(ts, "Five-quarter trend", "Reciprocal deposits and the cap each quarter ($000) for the banks this report features. "
           "Gap = reciprocal - cap; positive = over the cap.")
    heads = ["Institution", "Location", "Likely special cap", "Est. special cap ($000)"]
    for q in qs:
        heads += [f"{_qlab(q)} reciprocal", f"{_qlab(q)} cap", f"{_qlab(q)} gap"]
    heads += ["Gap change, first to last"]
    _header_row(ts, 4, heads)
    feat = [b for b in net.banks if len(b.history) >= 2]
    feat.sort(key=lambda b: -(b.gap_change() or -1e18))
    for i, nb in enumerate(feat, 5):
        vals = [nb.bank.name, nb.bank.location, "; ".join(nb.special_cap), nb.special_cap_est]
        for c, v in enumerate(vals, 1):
            ts.cell(row=i, column=c, value=v).font = _font(bold=(c == 1))
        ts.cell(row=i, column=4).number_format = "#,##0"
        first = last_ = None
        for j, q in enumerate(qs):
            c0 = 5 + 3 * j
            hh = _at(nb, q)
            ts.cell(row=i, column=c0, value=hh[1] if hh else None)
            ts.cell(row=i, column=c0 + 1, value=hh[2] if hh else None)
            L, M = get_column_letter(c0), get_column_letter(c0 + 1)
            ts.cell(row=i, column=c0 + 2, value=f'=IF(AND(ISNUMBER({L}{i}),ISNUMBER({M}{i})),{L}{i}-{M}{i},"")')
            for k in range(3):
                ts.cell(row=i, column=c0 + k).number_format = "#,##0;(#,##0);-"
                ts.cell(row=i, column=c0 + k).font = _font()
            if hh and hh[1] is not None and hh[2]:
                g = get_column_letter(c0 + 2)
                first = first or f"{g}{i}"
                last_ = f"{g}{i}"
        cc = ts.cell(row=i, column=5 + 3 * len(qs), value=f"={last_}-{first}" if first and last_ != first else None)
        cc.number_format = "#,##0;(#,##0);-"
        cc.font = _font(bold=True)
    if feat:
        col = get_column_letter(5 + 3 * len(qs))
        ts.conditional_formatting.add(f"{col}5:{col}{4 + len(feat)}", CellIsRule(operator="greaterThan", formula=["0"], fill=RED_FILL))
    _widths(ts, {"A": 34, "B": 22, "C": 24, "D": 14, **{get_column_letter(c): 13 for c in range(5, 6 + 3 * len(qs))}})
    ts.freeze_panes = "B5"

    for title, lst in (("Top 20 holders", net.top_exposure()), ("Near cap", net.near_cap()), ("Special cap", net.special()), ("Closest to special cap", net.closest_to_special()[:50]),
                       ("High uninsured", net.prospects())):
        ws = wb.create_sheet(title)
        _title(ws, title, CAP_NOTE if title != "High uninsured" else
               f"Uninsured {HIGH_UNINSURED:.0f}%+ of deposits, under half the cap used, not on the special cap.")
        _header_row(ws, 4, ["Institution", "Location", "Total assets", "Reciprocal ($000)", "% of cap", "Last quarter % of cap",
                            "Uninsured ($000)", "Uninsured share", "Likely special cap", "Call Report", "UBPR",
                            "Capital cushion (pts)", "Nearest line", "Est. special cap ($000)", "Above it ($000)"])
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
            for col_, v, fmt in ((12, nb.cushion, "+0.00;-0.00"), (13, nb.binding, "General"),
                                 (14, nb.special_cap_est, "#,##0"), (15, nb.over_special, "#,##0;(#,##0);-")):
                cell = ws.cell(row=i, column=col_, value=v)
                cell.font = _font()
                cell.number_format = fmt
        if not lst:
            ws.cell(row=5, column=1, value="None.").font = _font(italic=True)
        _widths(ws, {"A": 34, "B": 22, "C": 12, "D": 16, "E": 10, "F": 14, "G": 16, "H": 12, "I": 30, "J": 12, "K": 8,
                     "L": 12, "M": 22, "N": 16, "O": 14})
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
