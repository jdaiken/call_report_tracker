"""The Monday look book: the week in one page, charts with plain-English
analysis, regional bank stocks, the week ahead, and definitions of every rate.

Analysis is rules-based: fixed sentences filled in from the numbers, so it is
predictable and never invents anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Optional

from .analysis import Reading, notable_week
from .charts import Line, index_to_100, line_chart
from .email_report import (INK, LINE, QUIET, WATCH, Bundle, _bullets_html, _esc, _h2, _signed_pct,
                           _td, _th, calendar_html, extra_flags, headline_for, headlines_html, page,
                           summary_bullets)
from .series import SERIES, SPREADS


@dataclass
class ChartSection:
    cid: str
    title: str          # states the takeaway area, e.g. "Bank deposit rates vs. Treasuries"
    png: Optional[bytes]
    analysis: str


def _r(b: Bundle, key: str) -> Optional[Reading]:
    r = b.readings.get(key)
    return r if r and r.ok else None


def _wk(r: Optional[Reading]) -> str:
    return r.change_text("week") if r else "n/a"


def _pts(b: Bundle, key: str) -> list:
    r = _r(b, key)
    return r.points if r else []


# ------------------------------------------------------------- the analyses
def _rates_analysis(b: Bundle) -> str:
    effr, bill, mm, cd = (_r(b, k) for k in ("EFFR", "DGS3MO", "MMNDR", "NDR12MCD"))
    gap = _r(b, "bill_vs_mm")
    if not (effr and bill and mm):
        return "Not enough data this week to compare deposit rates with the Fed and Treasuries."
    s = (f"Fed funds is {effr.display_value} and 3-month T-bills pay {bill.display_value}. The average bank money "
         f"market account pays {mm.display_value}" + (f" and the average 12-month CD {cd.display_value}." if cd else "."))
    if gap:
        m = gap.changes.get("month")
        s += f" Treasuries out-yield the average money market account by {gap.value:.2f} points"
        if m is not None:
            s += f", {gap.change_text('month')} over the past month"
        s += ". "
        if m is not None and m >= 0.10:
            s += "Bank deposits are losing ground to Treasuries, which raises the pressure on banks to pay up or watch large balances leave."
        elif m is not None and m <= -0.10:
            s += "Bank deposits are becoming more competitive with Treasuries."
        else:
            s += "How bank deposits compare with Treasuries changed little this month."
    return s


def _deposits_analysis(b: Bundle) -> str:
    small, large, mmf = _r(b, "DPSSCBW027SBOG"), _r(b, "DPSLCBW027SBOG"), _r(b, "WRMFNS")
    bits = []
    for r, label in ((small, "small-bank deposits"), (large, "large-bank deposits"), (mmf, "retail money market funds")):
        if r and r.changes.get("year") is not None:
            bits.append(f"{label} are {r.change_text('year')}")
    if not bits:
        return "Deposit data didn't load this week."
    s = "Over the past year, " + ", ".join(bits) + "."
    if small and large and small.changes.get("year") is not None and large.changes.get("year") is not None:
        sg, lg = small.change_size("year"), large.change_size("year")
        if sg < lg - 1:
            s += " Large banks are gaining share at small banks' expense, the pattern IntraFi's products help smaller banks counter."
        elif sg > lg + 1:
            s += " Small banks are holding up better than large banks."
        else:
            s += " Small and large banks are growing at about the same pace."
    if mmf and small and mmf.change_size("year") > small.change_size("year") + 3:
        s += " Money funds are growing much faster than bank deposits, so savers are still choosing funds."
    if small and small.changes.get("week") is not None:
        s += f" This week, small-bank deposits were {_wk(small)}."
    return s


def _borrowing_analysis(b: Bundle) -> str:
    bor, dw = _r(b, "H8B3094NSMA"), _r(b, "WLCFLPCL")
    parts = []
    if bor:
        parts.append(f"Small banks have borrowed {bor.display_value}, mostly from the FHLBs. That is "
                     f"{bor.change_text('month')} over the past month and {bor.change_text('year')} over the past year.")
        if bor.change_size("month") >= 5:
            parts.append("That is a fast rise: small banks need more funding than their deposits provide.")
        elif bor.change_size("year") <= -5:
            parts.append("Borrowing is falling, a sign small banks' deposits are covering more of their needs.")
    if dw:
        parts.append(f"Emergency borrowing from the Fed's discount window is {dw.display_value} "
                     f"({dw.change_text('week')} this week).")
    return " ".join(parts) or "Borrowing data didn't load this week."


def _plumbing_analysis(b: Bundle) -> str:
    res, tga, rrp = _r(b, "WRESBAL"), _r(b, "WTREGEN"), _r(b, "RRPONTSYD")
    parts = []
    if res:
        parts.append(f"Bank reserves at the Fed are {res.display_value}, {res.change_text('month')} over the past month.")
    if tga:
        parts.append(f"The Treasury's cash account is {tga.display_value} ({tga.change_text('month')} over the month); "
                     "when it grows, money leaves the banking system.")
    if rrp:
        parts.append(f"Money parked at the Fed's reverse repo facility is {rrp.display_value}.")
        if rrp.value < 50:
            parts.append("With that cushion nearly empty, Treasury borrowing pulls directly from bank reserves.")
    return " ".join(parts) or "System cash data didn't load this week."


def _curve_analysis(b: Bundle) -> str:
    c2, c3 = _r(b, "curve_10y_2y"), _r(b, "curve_10y_3m")
    if not c2:
        return "Yield curve data didn't load this week."
    shape = "inverted (short rates above long rates)" if c2.value < 0 else "positively sloped (long rates above short rates)"
    s = f"The 10-year minus 2-year spread is {c2.display_value}, so the curve is {shape}"
    if c2.changes.get("month") is not None:
        s += f"; it is {c2.change_text('month')} over the past month"
    s += "."
    if c3:
        s += f" The 10-year minus 3-month spread, the Fed's preferred recession signal, is {c3.display_value}."
    s += (" A steeper curve helps bank margins because banks borrow short and lend long."
          if c2.value >= 0 else " An inverted curve squeezes bank margins and has often come before recessions.")
    return s


def _treasury_analysis(b: Bundle) -> str:
    parts = []
    for k, label in (("DGS2", "2-year"), ("DGS10", "10-year"), ("DGS30", "30-year")):
        r = _r(b, k)
        if r:
            parts.append(f"the {label} at {r.display_value} ({_wk(r)} this week)")
    if not parts:
        return "Treasury yield data didn't load this week."
    s = "Treasury yields: " + ", ".join(parts) + "."
    ten = _r(b, "DGS10")
    if ten and ten.changes.get("month") is not None and ten.changes["month"] >= 0.25:
        s += " Rising long-term yields lower the value of bonds banks hold, the issue that sank SVB."
    return s


def _credit_analysis(b: Bundle) -> str:
    hy, ig = _r(b, "BAMLH0A0HYM2"), _r(b, "BAMLC0A0CM")
    if not hy:
        return "Credit spread data didn't load this week."
    s = f"Junk-bond spreads are {hy.display_value} ({hy.change_text('month')} over the month)"
    if ig:
        s += f" and investment-grade spreads {ig.display_value}"
    s += ". "
    if hy.low_1y is not None and hy.high_1y is not None and hy.high_1y - hy.low_1y > 0.05:
        pos = (hy.value - hy.low_1y) / (hy.high_1y - hy.low_1y)
        s += ("Spreads are near their widest of the past year, so investors are worried about defaults."
              if pos > 0.8 else "Spreads are near their tightest of the past year, so credit markets are calm."
              if pos < 0.2 else "Spreads are in the middle of their past-year range.")
    return s


def _stocks_analysis(b: Bundle) -> str:
    parts = []
    for k, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        r = _r(b, k)
        if r:
            parts.append(f"{label} {_wk(r)} this week ({r.change_text('year')} over the year)")
    vix = _r(b, "VIXCLS")
    s = ("Stocks: " + "; ".join(parts) + ".") if parts else "Stock data didn't load this week."
    if vix:
        s += (f" The VIX is {vix.value:.1f}, " +
              ("a stressed level." if vix.value >= 25 else "elevated." if vix.value >= 20 else "calm."))
    return s


def _stablecoin_analysis(b: Bundle) -> str:
    pts = b.stablecoins.points
    if not pts:
        return "Stablecoin data didn't load this week."
    last_d, last_v = pts[-1]

    def ago(days: int) -> Optional[float]:
        older = [v for d, v in pts if d <= last_d - timedelta(days=days)]
        return older[-1] if older else None
    s = f"Stablecoins in circulation total ${last_v:,.0f} billion."
    for days, label in ((7, "week"), (30, "month"), (365, "year")):
        o = ago(days)
        if o:
            s += f" {_signed_pct((last_v / o - 1) * 100).capitalize()} over the past {label}."
    s += (" Every dollar here is a dollar that could have sat in a bank deposit; "
          "steady growth is the slow-moving deposit threat the GENIUS Act debate is about.")
    return s


def _cre_analysis(b: Bundle) -> str:
    cre = _r(b, "DRCRELEXFACBS")
    if not cre:
        return "CRE delinquency data didn't load this week."
    q = (cre.as_of.month - 1) // 3 + 1 if cre.as_of else None
    s = f"{cre.display_value} of commercial real estate loans were past due"
    s += f" in Q{q} {cre.as_of.year}" if q else ""
    s += f", {cre.change_text('last')} from the prior quarter and {cre.change_text('year')} from a year earlier."
    s += " This updates once a quarter; rising numbers point to future losses at community banks."
    return s


# ---------------------------------------------------------------- the charts
def build_charts(b: Bundle) -> list[ChartSection]:
    t = b.today
    y1, y2, y5 = t - timedelta(days=365), t - timedelta(days=730), t - timedelta(days=1830)
    specs: list[tuple[str, Callable[[], Optional[bytes]], Callable[[Bundle], str]]] = [
        ("Bank deposit rates vs. the Fed and Treasuries",
         lambda: line_chart([Line("Fed funds", _pts(b, "EFFR")), Line("3-mo T-bill", _pts(b, "DGS3MO")),
                             Line("Bank money market", _pts(b, "MMNDR")), Line("12-mo CD", _pts(b, "NDR12MCD"))], "pct", y1),
         _rates_analysis),
        ("Where deposits are going (indexed: one year ago = 100)",
         lambda: line_chart([Line("Small banks", index_to_100(_pts(b, "DPSSCBW027SBOG"), y1)),
                             Line("Large banks", index_to_100(_pts(b, "DPSLCBW027SBOG"), y1)),
                             Line("Money market funds", index_to_100(_pts(b, "WRMFNS"), y1))], "index", y1,
                            ref_line=(100, "")),
         _deposits_analysis),
        ("Small banks' borrowing (mostly FHLB advances)",
         lambda: line_chart([Line("Small-bank borrowings", _pts(b, "H8B3094NSMA"))], "billions", y2),
         _borrowing_analysis),
        ("Emergency borrowing from the Fed (discount window)",
         lambda: line_chart([Line("Discount window", _pts(b, "WLCFLPCL"))], "billions", y2),
         lambda _b: ""),
        ("Cash in the system: reserves, Treasury account, reverse repo",
         lambda: line_chart([Line("Bank reserves", _pts(b, "WRESBAL")), Line("Treasury account", _pts(b, "WTREGEN")),
                             Line("Reverse repo", _pts(b, "RRPONTSYD"))], "billions", y2),
         _plumbing_analysis),
        ("The yield curve",
         lambda: line_chart([Line("10-yr minus 2-yr", _pts(b, "curve_10y_2y")),
                             Line("10-yr minus 3-mo", _pts(b, "curve_10y_3m"))], "points", y2, zero_line=True),
         _curve_analysis),
        ("Treasury yields",
         lambda: line_chart([Line("2-yr", _pts(b, "DGS2")), Line("5-yr", _pts(b, "DGS5")),
                             Line("10-yr", _pts(b, "DGS10")), Line("30-yr", _pts(b, "DGS30"))], "pct", y1),
         _treasury_analysis),
        ("Credit spreads",
         lambda: line_chart([Line("High-yield", _pts(b, "BAMLH0A0HYM2")),
                             Line("Investment-grade", _pts(b, "BAMLC0A0CM"))], "pct", y1),
         _credit_analysis),
        ("Stocks (indexed: one year ago = 100)",
         lambda: line_chart([Line("S&P 500", index_to_100(_pts(b, "SP500"), y1)),
                             Line("Dow", index_to_100(_pts(b, "DJIA"), y1)),
                             Line("Nasdaq", index_to_100(_pts(b, "NASDAQCOM"), y1))], "index", y1,
                            ref_line=(100, "")),
         _stocks_analysis),
        ("Market stress gauge (VIX)",
         lambda: line_chart([Line("VIX", _pts(b, "VIXCLS"))], "level", y1, ref_line=(25, "stressed")),
         lambda _b: ""),
        ("Stablecoins in circulation",
         lambda: line_chart([Line("Stablecoin supply", b.stablecoins.points)], "billions", y2),
         _stablecoin_analysis),
        ("Commercial real estate loans past due",
         lambda: line_chart([Line("CRE delinquency rate", _pts(b, "DRCRELEXFACBS"))], "pct", y5, step=True),
         _cre_analysis),
    ]
    out = []
    for i, (title, draw, analyse) in enumerate(specs, 1):
        try:
            png = draw()
        except Exception:   # one bad chart never sinks the email
            png = None
        out.append(ChartSection(f"chart{i}", title, png, analyse(b)))
    return out


# ------------------------------------------------------------------ sections
def week_bullets(b: Bundle) -> list[tuple[str, str]]:
    flags = b.flags + extra_flags(b)
    out = summary_bullets(b, flags, max_flags=6)
    # Replace the daily market line with a weekly one.
    out = [x for x in out if not x[1].startswith("Markets on")]
    parts = []
    for k, label in (("SP500", "S&P 500"), ("DJIA", "Dow"), ("NASDAQCOM", "Nasdaq")):
        r = _r(b, k)
        if r and r.changes.get("week") is not None:
            parts.append(f"{label} {_wk(r)}")
    ten = _r(b, "DGS10")
    if ten and ten.changes.get("week") is not None:
        parts.append(f"10-year Treasury {ten.display_value} ({_wk(ten)})")
    if parts:
        out.insert(1, ("info", "This week: " + ", ".join(parts) + "."))
    for r in notable_week(b.readings, limit=4, covered=frozenset(k for f in flags for k in f.keys)):
        if r.key in ("SP500", "DJIA", "NASDAQCOM", "DGS10"):
            continue
        out.append(("move", f"{r.name} is {r.display_value}, {r.change_text('week')} this week."))
    year_fail = [f for f in b.failures.failures if f.fail_date.year == b.today.year]
    if not b.failures.error:
        out.append(("info", f"Bank failures so far in {b.today.year}: {len(year_fail)}"
                            + (f" (most recent: {year_fail[0].name}, {year_fail[0].fail_date:%b %-d})." if year_fail else ".")))
    return out


def regional_section(b: Bundle) -> str:
    rb = b.regional
    if not rb.ok:
        return (f'<p style="font-size:13px;color:{QUIET}">Regional bank stock data from Finviz didn\'t load this week'
                f"{' (' + _esc(rb.error[:120]) + ')' if rb.error else ''}.</p>")
    sp = _r(b, "SP500")
    wk = rb.cap_weighted_week
    s = (f"Across {len(rb.quotes)} regional banks worth over $300 million, the size-weighted average move was "
         f"{_signed_pct(wk)} this week" if wk is not None else
         f"Across {len(rb.quotes)} regional banks worth over $300 million, the size-weighted average move today was "
         f"{_signed_pct(rb.cap_weighted_change)}")
    if rb.cap_weighted_month is not None:
        s += f" and {_signed_pct(rb.cap_weighted_month)} over the month"
    if sp and sp.changes.get("week") is not None and wk is not None:
        s += f", compared with the S&P 500 {sp.change_text('week')} this week"
    s += "."
    worst = rb.worst(5, "perf_week_pct" if wk is not None else "change_pct")
    rows = "".join(
        "<tr>" + _td(_esc(q.company)) + _td(_esc(q.ticker), color=QUIET)
        + _td(f"{(q.perf_week_pct if wk is not None else q.change_pct):+.1f}%", "right", WATCH)
        + _td(f"${q.market_cap / 1e9:,.1f}B" if q.market_cap else "n/a", "right", QUIET) + "</tr>"
        for q in worst)
    table = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:8px">'
             f"<tr>{_th('Weakest this week' if wk is not None else 'Weakest today')}{_th('Ticker')}{_th('Change', 'right')}{_th('Market value', 'right')}</tr>{rows}</table>")
    return f'<p style="margin:0;font-size:14px;line-height:1.55;color:{INK}">{_esc(s)}</p>{table}'


def definitions_html() -> str:
    rows = ""
    for s in SERIES:
        rows += (f'<tr><td style="padding:8px 8px 8px 0;border-bottom:1px solid {LINE};vertical-align:top;font-size:13px;'
                 f'font-weight:600;color:{INK};width:32%">{_esc(s.name)}</td>'
                 f'<td style="padding:8px 0;border-bottom:1px solid {LINE};font-size:12.5px;line-height:1.5;color:{INK}">'
                 f'{_esc(s.what_it_is)} <span style="color:{QUIET}">{_esc(s.why_it_matters)}</span></td></tr>')
    for sp in SPREADS:
        rows += (f'<tr><td style="padding:8px 8px 8px 0;border-bottom:1px solid {LINE};vertical-align:top;font-size:13px;'
                 f'font-weight:600;color:{INK}">{_esc(sp.name)}</td>'
                 f'<td style="padding:8px 0;border-bottom:1px solid {LINE};font-size:12.5px;line-height:1.5;color:{INK}">'
                 f'{_esc(sp.what_it_is)} <span style="color:{QUIET}">{_esc(sp.reading)}</span></td></tr>')
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{rows}</table>'


def render_lookbook(b: Bundle) -> tuple[str, str, dict[str, bytes], str]:
    """Returns (html, text, images by content-id, subject)."""
    charts = build_charts(b)
    images = {c.cid: c.png for c in charts if c.png}

    inner = _h2("The week in one page")
    inner += f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.5">{_bullets_html(week_bullets(b))}</ul>'
    for c in charts:
        if not c.png and not c.analysis:
            continue
        inner += _h2(c.title)
        if c.png:
            inner += (f'<img src="cid:{c.cid}" width="624" alt="{_esc(c.title)} chart" '
                      f'style="display:block;width:100%;max-width:624px;height:auto;border:0;margin:0 0 6px">')
        else:
            inner += f'<p style="margin:0 0 6px;font-size:12px;color:{QUIET}">Chart unavailable this week.</p>'
        if c.analysis:
            inner += f'<p style="margin:0 0 4px;font-size:14px;line-height:1.55;color:{INK}">{_esc(c.analysis)}</p>'
    inner += _h2("Regional bank stocks", "From Finviz. Big moves here were the earliest warning in 2023.") + regional_section(b)
    inner += _h2("The week ahead") + calendar_html(b.calendar, b.today)
    if b.headlines or b.feed_errors:
        inner += _h2("From the Fed last week") + headlines_html(b.headlines, b.feed_errors)
    inner += _h2("Reference: what every rate means") + definitions_html()

    html = page("Weekly Look Book", f"Week of {b.today:%B %-d, %Y}", inner, b,
                footer_extra="Charts show the past one to two years (five for CRE). ")
    text = _lookbook_text(b, charts)
    flags = b.flags + extra_flags(b)
    subject = f"Weekly Look Book, {b.today:%b %-d}: {headline_for(flags, 'the week in rates, deposits and markets')}"
    return html, text, images, subject


def _lookbook_text(b: Bundle, charts: list[ChartSection]) -> str:
    lines = []
    if b.sample:
        lines.append("*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n")
    lines += [f"WEEKLY LOOK BOOK - week of {b.today:%B %-d, %Y}", "(Charts are in the HTML version of this email.)", "",
              "THE WEEK IN ONE PAGE"]
    lines += [f"  - {'WATCH: ' if lvl == 'watch' else ''}{t}" for lvl, t in week_bullets(b)]
    for c in charts:
        if c.analysis:
            lines += ["", c.title.upper(), f"  {c.analysis}"]
    lines += ["", "THE WEEK AHEAD"] + ([f"  {e.when:%a %b %-d}: {e.label}" for e in b.calendar.events] or ["  Nothing major scheduled."])
    lines += ["", "Data: FRED, FDIC, DeFiLlama, Finviz, federalreserve.gov. Not investment advice."]
    return "\n".join(lines)
