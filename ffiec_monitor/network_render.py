"""Email (highlights), ten-page PDF and spreadsheet for the weekly Network Exposure report (network.py)."""
from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from .network import (DISTRESS_RULES, GROUP_NOTES, RISK_MEASURES, SEGMENTS, distress_flags, issues, risk_score, EXCLUDE_ASSETS, HIGH_UNINSURED, NAME, NEAR_CAP, QUARTERS, TOP_N, Network, NetBank, _fmt_money, _pct,
                      short, takeaways)

PAGES = 7
RISK_NOTE = ("Reciprocal users with **two or more** of: over or near the cap, likely on (or over) the special cap, within "
             "0.5 pts of a capital minimum, a widening gap to the cap, 50%+ uninsured. Bar = reciprocal deposits at stake; "
             "the issues are listed beside it.")
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
    fo = net.focus()
    return [("BANKS IN REPORT", f"{len(fo):,}"), ("RECIPROCAL HELD", _fmt_money(sum(b.reciprocal or 0 for b in fo))),
            ("UNDER ORDER", f"{len(net.under_order()):,}"), ("LOWEST CAPITAL", f"{len(net.lowest_capital())}"),
            ("WEAK ASSETS", f"{len(net.weakest_assets())}"), ("ABOVE EST. SPECIAL CAP", f"{sum(1 for b in fo if b.special_cap and (b.over_special or 0) > 0)}")]


def md(text: str, bold_open: str = "<b>", bold_close: str = "</b>") -> str:
    """Escape text and turn **phrase** into bold."""
    import re as _re
    return _re.sub(r"\*\*(.+?)\*\*", lambda m: bold_open + m.group(1) + bold_close, escape(text))


# ------------------------------------------------------------------ email: highlights only
EMAIL_IMAGES = ("exposure",)


def render_html(net: Network) -> str:
    from . import network_style as S
    from .report import _e
    s = net.stats()
    B1 = f"<b style='font-weight:600;color:{S.NAVY}'>"
    FONT = "Inter,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
    heads = headlines(net)

    def kicker(t):
        return (f'<div style="font-size:11px;font-weight:600;letter-spacing:.12em;color:{S.MUTED};'
                f'text-transform:uppercase">{_e(t)}</div>')

    kp = "".join(
        f'<td valign="top" style="padding:12px 10px 12px 0;border-top:2px solid {S.NAVY}">'
        f'<div style="font-size:24px;font-weight:600;color:{S.NAVY};line-height:1.1">{_e(v)}</div>'
        f'<div style="font-size:10.5px;font-weight:500;letter-spacing:.04em;color:{S.MUTED};margin-top:4px">{_e(lab)}</div></td>'
        + ('<td width="10" style="border-top:2px solid #fff"></td>' if i < 5 else "")
        for i, (lab, v) in enumerate(_tiles(net)))
    b = f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{kp}</tr></table></td></tr>'
    if net.summary:
        b += (f'<tr><td style="padding:26px 0 0">{kicker("Summary")}<div style="font-size:15.5px;line-height:1.6;'
              f'color:{S.INK};margin-top:8px">{md(net.summary, B1, "</b>")}</div></td></tr>')
    if "exposure" in net.images:
        b += (f'<tr><td style="padding:30px 0 0">{kicker("Reciprocal exposure")}<div style="font-size:19px;font-weight:600;'
              f'color:{S.NAVY};margin-top:6px">{_e(heads[0])}</div><div style="font-size:13.5px;color:{S.MUTED};margin-top:4px">'
              f'Banks under order, with the lowest capital, the weakest asset quality, or on the treasury desk list. Navy tick = '
              f'estimated special cap; red = over it.</div></td></tr>'
              f'<tr><td style="padding:12px 0 0"><img src="cid:net_exposure" width="712" alt="Reciprocal exposure" '
              f'style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')
    toc = "".join(f'<tr><td style="padding:7px 0;border-bottom:1px solid {S.RULE};font-size:13.5px;color:{S.MUTED};width:34px">'
                  f'{i:02d}</td><td style="padding:7px 0;border-bottom:1px solid {S.RULE};font-size:13.5px;color:{S.INK}">'
                  f'<b style="font-weight:600">{_e(SECTIONS[i - 1])}</b> <span style="color:{S.MUTED}">· {_e(heads[i - 1])}</span>'
                  f'</td></tr>' for i in range(2, len(SECTIONS)))
    b += (f'<tr><td style="padding:30px 0 0">{kicker("In the attached report")}<table role="presentation" width="100%" '
          f'cellpadding="0" cellspacing="0" style="margin-top:6px">{toc}</table></td></tr>')
    cov = (f"{s['banks']:,} banks, latest Call Report ({net.quarter_label} where filed, else the prior quarter)"
           + (f"; {len(net.excluded)} banks of ${EXCLUDE_ASSETS // 1_000_000}B+ in assets left out" if net.excluded else "") + ".")
    b += (f'<tr><td style="padding:24px 0 0;font-size:12px;line-height:1.5;color:{S.MUTED}">Attached: seven-page PDF and a '
          f'spreadsheet with every bank. {_e(cov)} Sources: FFIEC Call Reports; FDIC, Federal Reserve, OCC and FinCEN '
          f'enforcement records.</td></tr>')
    return (f'<!doctype html><html><body style="margin:0;background:#f2f4f7;font-family:{FONT}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f7"><tr>'
            f'<td align="center" style="padding:22px 10px">'
            f'<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;width:100%;'
            f'background:#fff;border:1px solid {S.RULE}">'
            f'<tr><td style="padding:26px 24px 8px">{kicker("Weekly")}<div style="font-size:26px;font-weight:600;'
            f'color:{S.NAVY};margin-top:4px">{NAME}</div><div style="font-size:14px;color:{S.MUTED};margin-top:4px">'
            f'{net.run_date:%A, %B %-d, %Y}</div></td></tr>'
            f'<tr><td style="padding:14px 24px 28px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'{b}</table></td></tr></table></td></tr></table></body></html>')


def subject(net: Network) -> str:
    fo = net.focus()
    over = sum(1 for b in fo if b.special_cap and (b.over_special or 0) > 0)
    return (f"{NAME} · {net.run_date:%a %b %-d} · {len(fo)} banks · {len(net.under_order())} under order · "
            f"{over} above est. special cap · {_fmt_money(sum(b.reciprocal or 0 for b in fo))} reciprocal")


# ------------------------------------------------------------------ PDF (always seven pages)
SECTIONS = ["Summary", "Banks under order", "Lowest capital", "Weakest asset quality", "Treasury desk banks",
            "Reciprocal exposure and special caps", "Notes and sources"]


def headlines(net: Network) -> list[str]:
    """One plain sentence per page that says what the page shows (data-driven)."""
    s = net.stats()
    rk, top, near, close = net.risk(), net.top_exposure(), net.near_cap(), net.closest_to_special()
    tr = net.trend()
    wid = sum(1 for x in tr if x.gap_change() > 0)
    over_est = sum(1 for x in net.special() if (x.over_special or 0) > 0)
    neg = sum(1 for x in close if x.cushion < 0)
    thin = sum(1 for x in close if 0 <= x.cushion < 0.5)
    mv = net.movers()
    upm = max(mv, key=lambda x: x.reciprocal - x.prior_reciprocal, default=None)
    over_top = sum(1 for x in top if (x.used or 0) > 100)
    uo, lc, wa, tl, fo = net.under_order(), net.lowest_capital(), net.weakest_assets(), net.treasury_list(), net.focus()
    over = [x for x in fo if x.special_cap and (x.over_special or 0) > 0]
    return [
        f"{len(fo)} banks hold {_fmt_money(sum(x.reciprocal or 0 for x in fo))} in reciprocal deposits",
        f"{len(uo)} banks are under an active enforcement order",
        (f"Lowest capital: {short(lc[0].bank.name)} at {lc[0].leverage:.2f}% leverage" if lc else "No Q3 filers ranked yet"),
        (f"Weakest asset quality: {short(wa[0].bank.name)} at {wa[0].noncurrent:.1f}% noncurrent" if wa else "No Q3 filers ranked yet"),
        (f"{net.treasury_loaded - len(net.treasury_unmatched)} of the treasury desk's {net.treasury_loaded} banks found in "
         f"the Call Reports" if net.treasury_loaded
         else "The treasury desk's list hasn't been loaded yet"),
        f"{len(over)} order {'bank holds' if len(over) == 1 else 'banks hold'} more than our special-cap estimate",
        "How the measures are built",
    ]


def render_pdf(net: Network) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from . import network_style as S
    S.reportlab_fonts()
    C = colors.HexColor
    PW, PH = letter
    ML, MT, MB = 0.6 * inch, 0.95 * inch, 0.75 * inch
    WIDTH = PW - 2 * ML
    FRAME_H = PH - MT - MB - 14

    def st(name, size, color=S.INK, font="Inter", lead=None, **kw):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=lead or size * 1.32, textColor=C(color), **kw)
    T = {"kicker": st("k", 7.5, S.MUTED, "Inter-SemiBold"), "h": st("h", 17, S.NAVY, "Inter-SemiBold", 21),
         "lead": st("l", 9.5, S.MUTED, lead=13.5), "body": st("b", 10, S.INK, lead=14.5),
         "cell": st("c", 8.6), "cellb": st("cb", 8.6, S.INK, "Inter-SemiBold"), "sub": st("s", 7, S.MUTED),
         "num": st("n", 8.6, alignment=TA_RIGHT), "numb": st("nb", 8.6, S.INK, "Inter-SemiBold", alignment=TA_RIGHT),
         "th": st("th", 6.8, S.MUTED, "Inter-SemiBold"), "thr": st("thr", 6.8, S.MUTED, "Inter-SemiBold", alignment=TA_RIGHT),
         "note": st("nt", 7.6, S.MUTED, lead=10.5), "kpi": st("kpi", 17, S.NAVY, "Inter-SemiBold", 21),
         "kpil": st("kpil", 7, S.MUTED, "Inter-Medium", 9), "title": st("t", 26, S.NAVY, "Inter-SemiBold", 30)}
    p = lambda t, k: Paragraph(t, T[k])                                       # noqa: E731
    BOLD_NAVY = (f"<font name='Inter-SemiBold' color='{S.NAVY}'>", "</font>")
    heads = headlines(net)
    s = net.stats()

    def section(n):
        return [p(f"{n:02d}&nbsp;&nbsp;&nbsp;{escape(SECTIONS[n - 1].upper())}", "kicker"), Spacer(1, 5),
                p(escape(heads[n - 1]), "h"), Spacer(1, 6)]

    def lead(text):
        return [p(md(text, f"<font name='Inter-SemiBold' color='{S.INK}'>", "</font>"), "lead"), Spacer(1, 10)]

    def chart(cid, max_h):
        if cid not in net.images:
            return []
        im = Image(io.BytesIO(net.images[cid]))
        w = WIDTH
        h = w * im.imageHeight / im.imageWidth
        if h > max_h * inch:
            h = max_h * inch
            w = h * im.imageWidth / im.imageHeight
        im.drawWidth, im.drawHeight = w, h
        im.hAlign = "LEFT"
        return [im, Spacer(1, 12)]

    def bank_cell(nb):
        return p(f"{escape(short(nb.bank.name, 40))}<br/><font name='Inter' size='6.8' color='{S.MUTED}'>"
                 f"{escape(nb.bank.location)} · {escape(nb.bank.assets)}</font>", "cellb")

    def tbl(rows, cols):
        fixed = sum(w for _, w, _ in cols if w)
        widths = [(w * inch) if w else (WIDTH - fixed * inch) for _, w, _ in cols]
        head = [p(lab, "th" if i == 0 else "thr") for i, (lab, _, _) in enumerate(cols)]
        data = [head] + [[fn(nb) for _, _, fn in cols] for nb in rows]
        t = Table(data, colWidths=widths, repeatRows=1)
        style = [("LINEBELOW", (0, 0), (-1, 0), 0.8, C(S.NAVY)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
                 ("LEFTPADDING", (0, 0), (0, -1), 0)]
        style += [("LINEBELOW", (0, i), (-1, i), 0.4, C(S.RULE)) for i in range(1, len(data))]
        t.setStyle(TableStyle(style))
        return t

    def more(shown, total):
        return [Spacer(1, 5), p(f"Top {shown} of {total} shown. All {total} are in the attached spreadsheet.", "note")] if total > shown else []

    def none(text="None this week."):
        return [p(text, "body")]

    num = lambda t: p(t, "num")                                               # noqa: E731
    numb = lambda t: p(t, "numb")                                             # noqa: E731
    BANK = ("BANK", None, bank_cell)
    REC = ("RECIPROCAL", 0.95, lambda nb: numb(_fmt_money(nb.reciprocal)))
    CAP = ("CAP", 0.85, lambda nb: num(_fmt_money(nb.cap)))
    USED = ("% OF CAP", 0.8, lambda nb: num(_pct(nb.used)))
    YAGO = ("A YEAR AGO", 0.85, lambda nb: num(_pct(nb.year_ago_used()) if nb.year_ago_used() is not None else "—"))
    UNI = ("UNINSURED", 0.8, lambda nb: num(_pct(nb.uninsured_share)))
    EST = ("EST. SPECIAL CAP", 1.1, lambda nb: num(_fmt_money(nb.special_cap_est) if nb.special_cap else "—"))
    ABOVE = ("ABOVE IT", 0.8, lambda nb: num(_over(nb) or "—"))

    def money_delta(v):
        return "—" if v is None else f"{'+' if v >= 0 else '-'}{_fmt_money(abs(v))}"

    GAPCH = ("GAP, 1-YR CHANGE", 1.15, lambda nb: num(money_delta(nb.gap_change())))
    SPCH = ("1-YR CHANGE", 0.95, lambda nb: num(money_delta(nb.special_gap_change())))

    # 1. Summary (cover)
    kpis = _tiles(net)
    cells = [[p(v, "kpi") for _, v in kpis], [p(lab, "kpil") for lab, _ in kpis]]
    kw = WIDTH / len(kpis)
    kt = Table(cells, colWidths=[kw] * len(kpis))
    kt.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.8, C(S.NAVY)), ("LINEBELOW", (0, -1), (-1, -1), 0.4, C(S.RULE)),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, 0), 10),
                            ("BOTTOMPADDING", (0, -1), (-1, -1), 10), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    tk = takeaways(net)

    def p1(cap):
        out = [p("WEEKLY", "kicker"), Spacer(1, 4), p(NAME, "title"), Spacer(1, 4),
               p(f"{net.run_date:%B %-d, %Y} · Call Reports for {net.quarter_label} where filed, else the prior quarter", "lead"),
               Spacer(1, 16), kt, Spacer(1, 18)]
        if net.summary:
            out += [p("SUMMARY", "kicker"), Spacer(1, 5), p(md(net.summary, *BOLD_NAVY), "body"), Spacer(1, 16)]
        out += [p("KEY POINTS", "kicker"), Spacer(1, 6)]
        for i, t in enumerate(tk[:max(1, min(cap, len(tk)))], 1):
            row = Table([[p(f"{i}", "cellb"), p(md(t, *BOLD_NAVY), "body")]], colWidths=[0.28 * inch, WIDTH - 0.28 * inch])
            row.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            out.append(row)
        if cap > len(tk):
            out += [Spacer(1, 12), p("CONTENTS", "kicker"), Spacer(1, 5)]
            toc = [[p(f"{i:02d}", "note"), p(escape(t), "note")] for i, t in enumerate(SECTIONS[1:], 2)]
            half = (len(toc) + 1) // 2
            grid = [toc[i] + (toc[i + half] if i + half < len(toc) else ["", ""]) for i in range(half)]
            g = Table(grid, colWidths=[0.3 * inch, WIDTH / 2 - 0.3 * inch] * 2)
            g.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
            out.append(g)
        return out

    rk, top, near = net.risk(), net.top_exposure(), net.near_cap()
    spec, close, pro, mv = net.special(), net.closest_to_special(), net.prospects(), net.movers()

    uo, lc, wa, tl, fo = net.under_order(), net.lowest_capital(), net.weakest_assets(), net.treasury_list(), net.focus()
    LEV = ("LEVERAGE", 0.8, lambda nb: num(_pct(nb.leverage)))
    NCL = ("NONCURRENT", 0.85, lambda nb: num(_pct(nb.noncurrent)))
    ROA = ("ROA", 0.65, lambda nb: num(_pct((nb.ratios or {}).get("roaa"))))
    TRBC = ("TOTAL RBC", 0.8, lambda nb: num(_pct((nb.capital or {}).get("total_rbc_ratio"))))
    WHY = ("IN REPORT FOR", 1.45, lambda nb: p(escape(", ".join(net.reasons(nb))), "note"))

    def order_text(nb):
        o = sorted(nb.orders, key=lambda a: a.start or date.min, reverse=True)[0]
        extra = f" (+{len(nb.orders) - 1} more)" if len(nb.orders) > 1 else ""
        return p(escape(f"{o.agency} {o.action}" + (f", {o.start:%b %Y}" if o.start else "") + extra), "note")

    def p2(cap):
        out = section(2) + lead("Every bank matched to an active FDIC, Fed, OCC or FinCEN order (civil money penalties and "
                                "orders over 10 years old left out). **A bank under order is held to its special cap**: bar = "
                                "reciprocal deposits; navy tick = estimated special cap; red = over it.") + chart("orders", 3.2)
        ordc = ("ORDER", 1.9, order_text)
        return out + ([tbl(uo[:cap], [BANK, ordc, LEV, NCL, REC, EST])] + more(min(cap, len(uo)), len(uo)) if uo else none())

    def p3(cap):
        out = section(3) + lead(f"The {len(lc)} Q3 filers with the **lowest Tier 1 leverage ratio** (banks under $50 million "
                                "left out). Dashed line = 5%, the well-capitalized minimum (community bank leverage ratio "
                                "banks need 9%). Red = under 5%; amber = 5-6%.") + chart("capital", 4.3)
        return out + ([tbl(lc[:cap], [BANK, LEV, TRBC, NCL, REC, USED])] + more(min(cap, len(lc)), len(lc)) if lc else none())

    def p4(cap):
        out = section(4) + lead(f"The {len(wa)} Q3 filers with the **highest noncurrent loans plus foreclosed real estate** "
                                "(loans 90+ days past due or not accruing, plus OREO, as a share of loans + OREO; banks under "
                                "$50 million left out). Red = 5% or more; amber = 3-5%.") + chart("assets", 4.3)
        return out + ([tbl(wa[:cap], [BANK, NCL, ROA, LEV, REC, USED])] + more(min(cap, len(wa)), len(wa)) if wa else none())

    def p5(cap):
        out = section(5)
        if not net.treasury_loaded:
            return out + lead("**The treasury desk's list hasn't been loaded yet** (data/treasury-desk.csv).")
        out += lead("The banks on the Treasury Desk alert list, with the same measures as the rest of the report. **These are "
                    "kept even above the $25 billion cutoff**, since the desk asked for them by name.")
        if net.treasury_unmatched:
            out += [p(escape("No Call Report this quarter: " + ", ".join(net.treasury_unmatched)
                             + ". U.S. branches of foreign banks file the FFIEC 002 instead; others may not have filed Q3 yet."),
                      "note"), Spacer(1, 4)]
        multi = net.treasury_multi()
        if multi:
            out += [p(escape("Generic names that matched more than one bank (all shown; pin a state in the list to narrow): "
                             + "; ".join(f"{k} ({len(v)})" for k, v in multi.items())), "note"), Spacer(1, 6)]
        return out + ([tbl(tl[:cap], [BANK, LEV, NCL, ROA, REC, USED, EST])] + more(min(cap, len(tl)), len(tl)) if tl else none())

    def p6(cap):
        out = section(6) + lead("Every bank in this report that holds reciprocal deposits. **Only banks under an active order "
                                "get a special-cap estimate** (navy tick); red = balance above that estimate, which is a "
                                "question to ask, not a finding.") + chart("exposure", 4.4)
        withrec = [b for b in fo if (b.reciprocal or 0) > 0]
        return out + ([tbl(withrec[:cap], [BANK, WHY, REC, USED, EST, ABOVE])] + more(min(cap, len(withrec)), len(withrec))
                      if withrec else none("None of these banks holds reciprocal deposits."))

    def p7(_cap):
        out = section(7)
        for t in ("**Reciprocal cap.** Reciprocal deposits count as non-brokered only up to the lesser of $5 billion or 20% "
                  "of total liabilities (12 CFR 337.6(e)). Above that, further placements count as brokered.",
                  "**Special cap: what it is.** No regulator publishes or assigns a number. It is set by formula in the FDIC's "
                  "brokered-deposit rule (12 CFR 337.6(e)): a bank that is not both well capitalized and well rated (a "
                  "composite exam rating of 1 or 2) may take reciprocal deposits as non-brokered only up to the average of its "
                  "quarter-end reciprocal deposits over the **four quarters before the quarter it slipped**. Capital comes "
                  "from the Call Report; exam ratings are confidential.",
                  "**Special cap: who gets one here.** Only banks under an active FDIC, Fed, OCC or FinCEN order. Banks "
                  "below well capitalized without an order are flagged on capital but get no special-cap estimate.",
                  "**Special cap: how we estimate it.** We take the quarter of the bank's earliest active order as the "
                  "quarter it slipped and **average its reported reciprocal deposits (RC-E) for the four quarter-ends before "
                  "it**. The real downgrade usually comes months before the order, so this is an estimate: a balance above it "
                  "is worth a question, not a finding. A bank downgraded privately (no public order) can't be seen.",
                  "**Who is in this report.** Every bank under an active FDIC, Fed, OCC or FinCEN order; the 25 Q3 2026 "
                  "filers with the lowest Tier 1 leverage ratio; the 25 with the highest noncurrent loans plus foreclosed "
                  "real estate (both rankings skip banks under $50 million); and the treasury desk's list.",
                  "**Asset quality.** Loans 90+ days past due plus nonaccrual loans plus other real estate owned, divided "
                  "by loans plus OREO (Call Report RC-N and RC). Return on assets annualizes the year-to-date figure.",
                  "**Scope.** " + EXCLUDE_NOTE + f" Coverage: {s['banks']:,} banks"
                  + (f"; {len(net.excluded)} left out" if net.excluded else "")
                  + (f"; {net.missing:,} not yet loaded" if net.missing else "") + ".",
                  "**Sources.** FFIEC Call Reports (RC-E reciprocal deposits, RC-O uninsured deposits, RC total liabilities, "
                  "RC-R capital, RC-N past due and nonaccrual); FDIC, Federal Reserve, OCC and FinCEN enforcement records. "
                  "UBPR and Call Report links for every bank are in the spreadsheet."):
            out += [p(md(t, *BOLD_NAVY), "body"), Spacer(1, 8)]
        return out

    def height(fl):
        return sum(f.wrap(WIDTH, PH)[1] + f.getSpaceBefore() + f.getSpaceAfter() for f in fl)

    def fit(build, start):
        for cap in range(start, -1, -1):
            pg = build(cap)
            if height(pg) <= FRAME_H:
                return pg
        pg = build(0)
        for _ in range(12):                                   # still too tall: shrink its charts
            over = height(pg) - FRAME_H
            ims = [f for f in pg if isinstance(f, Image)]
            if over <= 0 or not ims:
                break
            tot = sum(f.drawHeight for f in ims)
            k = max(0.5, (tot - over - 4) / tot)
            for f in ims:
                f.drawHeight *= k
                f.drawWidth *= k
        return pg

    pages = [fit(p1, len(tk) + 1), fit(p2, 12), fit(p3, 12), fit(p4, 12), fit(p5, 25), fit(p6, 12), fit(p7, 0)]
    story = []
    for i, pg in enumerate(pages):
        story += pg + ([PageBreak()] if i < len(pages) - 1 else [])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=ML, rightMargin=ML, topMargin=MT, bottomMargin=MB,
                            title=f"{NAME} {net.run_date:%Y-%m-%d}", author=NAME)

    def chrome(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(C(S.NAVY))
        canvas.setFont("Inter-SemiBold", 7.5)
        canvas.drawString(ML, PH - 0.5 * inch, NAME.upper())
        canvas.setFillColor(C(S.MUTED))
        canvas.setFont("Inter", 7.5)
        canvas.drawRightString(PW - ML, PH - 0.5 * inch, f"{net.run_date:%B %-d, %Y}")
        canvas.setStrokeColor(C(S.RULE))
        canvas.setLineWidth(0.6)
        canvas.line(ML, PH - 0.58 * inch, PW - ML, PH - 0.58 * inch)
        canvas.line(ML, 0.55 * inch, PW - ML, 0.55 * inch)
        canvas.setFont("Inter", 7)
        canvas.drawString(ML, 0.38 * inch, "Source: FFIEC Call Reports; FDIC, Federal Reserve, OCC and FinCEN enforcement records.")
        canvas.setFont("Inter-SemiBold", 7.5)
        canvas.setFillColor(C(S.NAVY))
        canvas.drawRightString(PW - ML, 0.38 * inch, f"{doc_.page}  /  {PAGES}")
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
             "Capital cushion (pts)", "Nearest line", "Est. special cap (4Q avg)", "Above est. special cap", "Watch-list issues",
             "Signs of distress", "Special cap estimate based on"]
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
        for c, v in ((24, "; ".join(issues(nb)) if (nb.reciprocal or 0) > 0 else ""),
                     (25, "; ".join(f"{k}: {v}" for k, v in distress_flags(nb).items())), (26, nb.special_basis)):
            bs.cell(row=r, column=c, value=v).font = _font()
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
                 "M": 13, "N": 10, "O": 26, "P": 11, "Q": 12, "R": 8, "S": 12, "T": 11, "U": 22, "V": 15, "W": 15, "X": 40, "Y": 44, "Z": 50})

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
