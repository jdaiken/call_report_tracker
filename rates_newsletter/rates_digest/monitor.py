"""Rates & Deposit Risk Monitor: a fixed five-page daily report.

  1 Summary                        status counts, key signals, every risk measure in one table
  2 Rates                          every daily rate in one table; Fed path; Treasuries vs. bank deposits
  3 Deposits & competition         deposit flight, money funds, stablecoins, small-bank borrowing
  4 Funding & credit stress        overnight funding, discount window, credit spreads, CRE
  5 Markets & calendar             10-year, yield curve, VIX, bank failures, coming up

`build_report()` produces pure content; `render_html()` (the email) and
pdf.render_pdf() (the attachment) both draw from it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional

from .analysis import Reading
from .charts import Band, Line, index_to_100, line_chart
from .email_report import Bundle, _esc
from .extras import FOMC_DECISION_DAYS, Event, Failure, deposits_note, fmt_thousands
from .newsletter import (FDIC_FAILURES_URL, ITEMS, OK_C, RISK_C, TONE_COLOR, TONE_WORD, WATCH_C,
                         _pct, _r, current_zone, metric_value, sources_for, DEFILLAMA_URL)

NAME = "Rates & Deposit Risk Monitor"
INK, MUTED, RULE, PANEL_BG, PAGE_BG, LINK = "#14181f", "#5b626c", "#dde0e4", "#ffffff", "#eef0f3", "#1d5fbf"
FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


# ------------------------------------------------------------------ signals
def _sign(v: float, unit: str = "") -> str:
    return f"{'+' if v >= 0 else '−'}{abs(v):.2f}{unit}" if unit != "%" else f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def signal(key: str, r: Reading, b: Bundle) -> str:
    """One short line: the status in words plus the single most useful context."""
    item = ITEMS[key]
    z = current_zone(item, r)
    head = z.short[0].upper() + z.short[1:] if z else ""
    if key == "DFEDTARU":
        yr = r.changes.get("year")
        nxt = next((d for d in FOMC_DECISION_DAYS if d >= b.today), None)
        bits = []
        if yr is not None:
            bits.append("unchanged over 12 mo" if abs(yr) < 0.01 else f"{_sign(yr)} pts over 12 mo")
        if nxt:
            bits.append(f"next decision {nxt:%b %-d}")
        return " · ".join(bits)
    if key == "DGS2":
        effr = _r(b, "EFFR")
        if not effr:
            return ""
        d = r.value - effr.value
        view = "Markets price cuts" if d <= -0.25 else "Markets price hikes" if d >= 0.25 else "Markets price a hold"
        return f"{view} · {_sign(d)} pts vs. fed funds"
    ctx = ""
    if key == "bill_vs_mm":
        bill, mm = _r(b, "DGS3MO"), _r(b, "MMNDR")
        ctx = f"T-bills {bill.display_value} vs. banks {mm.display_value}" if bill and mm else ""
    elif key == "DPSSCBW027SBOG":
        sg, lg = _pct(r, "year"), _pct(_r(b, "DPSLCBW027SBOG"), "year")
        ctx = f"Small {sg:+.1f}% vs. largest {lg:+.1f}% (12 mo)" if sg is not None and lg is not None else ""
    elif key == "STABLECOINS":
        small = _r(b, "DPSSCBW027SBOG")
        m = metric_value(item, r)
        ctx = " · ".join(x for x in (f"{m:+.0f}% over 12 mo" if m is not None else "",
                                      f"{r.value / small.value * 100:.1f}% of small-bank deposits" if small else "") if x)
    elif key == "DRCRELEXFACBS" and r.as_of:
        ctx = f"Q{(r.as_of.month - 1) // 3 + 1} {r.as_of.year}"
    elif key == "VIXCLS" and r.low_1y is not None:
        ctx = f"12-mo range {r.low_1y:.1f}–{r.high_1y:.1f}"
    elif item.metric == "yoy":
        m = metric_value(item, r)
        ctx = f"{m:+.1f}% over 12 mo" if m is not None else ""
    elif r.changes.get("month") is not None:
        ctx = f"{r.compact_change('month')} over 1 mo"
    return " · ".join(x for x in (head, ctx) if x)


# --------------------------------------------------------------- layout spec
@dataclass
class PanelSpec:
    key: str                          # the measure whose status and reading head the panel
    title: str
    lines: list[tuple[str, str]]      # chart series: (label, reading key)
    kind: str                         # pct | points | billions | index | level
    years: float
    index: bool = False
    zero: bool = False
    step: bool = False
    fed_band: bool = False
    ref: Optional[tuple[float, str]] = None
    height: float = 3.8               # chart height in inches (page 5 runs shorter)


@dataclass
class PageSpec:
    title: str
    panels: list[PanelSpec]


# Page 2: every rate in one table, then two charts.
RATE_GROUPS = [
    ("Policy & overnight", [("DFEDTARU", "Fed funds target range"), ("EFFR", "Effective fed funds"),
                            ("IORB", "Interest on reserves (IORB)"), ("SOFR", "SOFR"), ("DPRIME", "Prime rate")]),
    ("Treasuries", [("DGS1MO", "1-month bill"), ("DGS3MO", "3-month bill"), ("DGS6MO", "6-month bill"),
                    ("DGS1", "1-year"), ("DGS2", "2-year"), ("DGS10", "10-year")]),
    ("Bank deposit rates · FDIC national averages", [("SNDR", "Savings"), ("MMNDR", "Money market"),
                                                    ("NDR6MCD", "6-month CD"), ("NDR12MCD", "12-month CD")]),
    ("FDIC rate caps", [("MMNRC", "Money market cap"), ("NRC12MCD", "12-month CD cap")]),
]
RATES_PANELS = [
    PanelSpec("DFEDTARU", "Fed policy path", [("Fed funds", "EFFR"), ("2-year", "DGS2")], "pct", 2, fed_band=True),
    PanelSpec("bill_vs_mm", "Treasuries vs. bank deposits", [("Gap", "bill_vs_mm")], "points", 3),
]
CHART_PAGES = [
    PageSpec("Deposits & competition", [
        PanelSpec("DPSSCBW027SBOG", "Deposit flight (12 mo, indexed)",
                  [("Small", "DPSSCBW027SBOG"), ("Largest", "DPSLCBW027SBOG"), ("MMFs", "WRMFNS")], "index", 1,
                  index=True, ref=(100.0, "")),
        PanelSpec("WRMFNS", "Money market funds", [("MMF assets", "WRMFNS")], "billions", 3),
        PanelSpec("STABLECOINS", "Stablecoins in circulation", [("Stablecoins", "STABLECOINS")], "billions", 3),
        PanelSpec("H8B3094NSMA", "Small-bank borrowing", [("Borrowings", "H8B3094NSMA")], "billions", 3),
    ]),
    PageSpec("Funding & credit stress", [
        PanelSpec("sofr_vs_iorb", "Overnight funding (SOFR − IORB)", [("SOFR − IORB", "sofr_vs_iorb")], "points", 2, zero=True),
        PanelSpec("WLCFLPCL", "Discount window borrowing", [("Discount window", "WLCFLPCL")], "billions", 4),
        PanelSpec("BAMLH0A0HYM2", "High-yield credit spread", [("HY spread", "BAMLH0A0HYM2")], "pct", 3),
        PanelSpec("DRCRELEXFACBS", "CRE loans past due", [("CRE past due", "DRCRELEXFACBS")], "pct", 10, step=True),
    ]),
]
LAST_PAGE_PANELS = [
    PanelSpec("DGS10", "10-year Treasury", [("10-year", "DGS10")], "pct", 3, height=3.2),
    PanelSpec("curve_10y_2y", "Yield curve (10y − 2y)", [("Curve", "curve_10y_2y")], "points", 3, zero=True, height=3.2),
    PanelSpec("VIXCLS", "Market volatility (VIX)", [("VIX", "VIXCLS")], "level", 2, height=3.2),
]

# Summary table order, grouped as the pages are.
SUMMARY_GROUPS = [
    ("Rates", ["DFEDTARU", "DGS2", "bill_vs_mm", "DGS10", "curve_10y_2y"]),
    ("Deposits & competition", ["DPSSCBW027SBOG", "WRMFNS", "STABLECOINS", "H8B3094NSMA"]),
    ("Funding & credit stress", ["sofr_vs_iorb", "WLCFLPCL", "BAMLH0A0HYM2", "DRCRELEXFACBS"]),
    ("Market stress", ["VIXCLS"]),
]


# ------------------------------------------------------------------ content
@dataclass
class Row:
    key: str
    name: str
    reading: str
    tone: str
    signal: str
    last: str
    month: str
    year: str
    url: Optional[str]
    loaded: bool = True


@dataclass
class RateRow:
    name: str
    latest: str
    day: str
    week: str
    month: str
    year: str
    as_of: str
    url: Optional[str]


@dataclass
class Panel:
    key: str
    cid: str
    title: str
    reading: str
    tone: str
    signal: str
    png: Optional[bytes]
    url: Optional[str]
    sources: list[tuple[str, str]] = field(default_factory=list)   # (short label, url) for every series charted


def panel_sources(spec: PanelSpec) -> list[tuple[str, str]]:
    """Every series behind a panel's number and chart, as (FRED id or 'DeFiLlama', url), no repeats."""
    out: list[tuple[str, str]] = []
    keys = [spec.key] + [k for _, k in spec.lines] + (["DFEDTARL"] if spec.fed_band else [])
    for k in keys:
        for label, url in sources_for(k):
            short = "DeFiLlama" if url == DEFILLAMA_URL else label.rsplit("(", 1)[-1].rstrip(")")
            if (short, url) not in out:
                out.append((short, url))
    return out


@dataclass
class Report:
    today: date
    data_through: Optional[date]
    subject: str
    counts: dict[str, int]
    signals: list[tuple[str, str]]               # (tone, text), most severe first
    groups: list[tuple[str, list[Row]]]
    rates: list[tuple[str, list[RateRow]]]       # page 2 table
    rates_panels: list[Panel]                    # page 2 charts
    pages: list[tuple[str, list[Panel]]]         # chart pages 3-4
    last_panels: list[Panel]                     # page 5 charts
    failures_ytd: Optional[int]
    recent_failures: list[Failure]
    coming_up: list[Event]
    sources: list[tuple[str, list[tuple[str, str]]]]
    sample: bool
    missing: list[str] = field(default_factory=list)
    summary: str = ""                 # plain-English paragraph for page 1
    summary_by: str = ""              # "Claude" or "rules"

    @property
    def all_panels(self) -> list[Panel]:
        return self.rates_panels + [p for _, ps in self.pages for p in ps] + self.last_panels


def _reading(key: str, r: Reading, b: Bundle) -> str:
    if key == "DFEDTARU":
        bot = _r(b, "DFEDTARL")
        return f"{bot.value:.2f}–{r.value:.2f}%" if bot else r.display_value
    return r.display_value.replace(" points", " pts").replace(" trillion", "T").replace(" billion", "B")


def _row(key: str, b: Bundle) -> Row:
    item = ITEMS[key]
    r = b.readings.get(key)
    url = sources_for(key)[0][1] if sources_for(key) else None
    if r is None or not r.ok:
        return Row(key, item.name, "n/a", "info", "Data unavailable", "", "", "", url, loaded=False)
    z = current_zone(item, r)
    return Row(key, item.name, _reading(key, r, b), z.tone if z else "info", signal(key, r, b),
               r.compact_change("last"), r.compact_change("month"), r.compact_change("year"), url)


def _rate_row(key: str, label: str, b: Bundle) -> RateRow:
    from .newsletter import FRED_URL
    r = b.readings.get(key)
    url = FRED_URL.format(key)
    if r is None or not r.ok:
        return RateRow(label, "n/a", "", "", "", "", "", url)
    latest = _reading(key, r, b)

    def ch(w: str) -> str:
        return r.compact_change(w).replace(" pts", "") if r.changes.get(w) is not None else "—"
    return RateRow(label, latest, ch("last") if (b.today - r.as_of).days <= 5 and r.changes.get("last") is not None
                   and (r.as_of - (r.prev_date or r.as_of)).days <= 4 else "—",
                   ch("week"), ch("month"), ch("year"), f"{r.as_of:%b %-d}", url)


def _chart(spec: PanelSpec, b: Bundle) -> Optional[bytes]:
    start = b.today - timedelta(days=int(365 * spec.years))
    lines = []
    for label, key in spec.lines:
        r = b.readings.get(key)
        pts = r.points if r and r.ok else []
        lines.append(Line(label, index_to_100(pts, start) if spec.index else pts))
    item = ITEMS.get(spec.key)
    bands = []
    if item and item.metric == "value" and not spec.index:
        bands = [Band(z.lo, z.hi, z.tone, f"{TONE_WORD[z.tone]}: {z.short}") for z in item.zones if z.tone != "ok"]
    fed = None
    if spec.fed_band and _r(b, "DFEDTARU") and _r(b, "DFEDTARL"):
        fed = (b.readings["DFEDTARL"].points, b.readings["DFEDTARU"].points, "Fed target range")
    try:
        return line_chart(lines, spec.kind, start, zero_line=spec.zero, ref_line=spec.ref, step=spec.step,
                          bands=bands, range_band=fed, compact=True, height=spec.height)
    except Exception:
        return None


def _panel(spec: PanelSpec, b: Bundle, n: int) -> Panel:
    row = _row(spec.key, b)
    return Panel(spec.key, f"chart{n}", spec.title, row.reading, row.tone, row.signal, _chart(spec, b), row.url, panel_sources(spec))


def build_report(b: Bundle) -> Report:
    groups = [(title, [_row(k, b) for k in keys]) for title, keys in SUMMARY_GROUPS]
    rows = [r for _, rs in groups for r in rs]
    counts = {t: sum(1 for r in rows if r.tone == t) for t in ("risk", "watch", "ok")}

    rates = [(title, [_rate_row(k, label, b) for k, label in keys]) for title, keys in RATE_GROUPS]
    n = 0
    rates_panels = []
    for spec in RATES_PANELS:
        n += 1
        rates_panels.append(_panel(spec, b, n))
    pages = []
    for p in CHART_PAGES:
        panels = []
        for spec in p.panels:
            n += 1
            panels.append(_panel(spec, b, n))
        pages.append((p.title, panels))
    last_panels = []
    for spec in LAST_PAGE_PANELS:
        n += 1
        last_panels.append(_panel(spec, b, n))

    signals: list[tuple[str, str]] = []
    # All failures this year, newest first (shown on page 5); a failure in the past week is also a key signal.
    recent = sorted((f for f in b.failures.failures if f.fail_date.year == b.today.year),
                    key=lambda f: f.fail_date, reverse=True)
    for f in recent:
        if (b.today - f.fail_date).days <= 7:
            signals.append(("risk", f"Bank failure: {f.name} ({f.city_state}), {f.fail_date:%b %-d}, "
                                    f"{fmt_thousands(f.deposits_thousands)} deposits" if f.deposits_thousands is not None
                                    else f"Bank failure: {f.name} ({f.city_state}), {f.fail_date:%b %-d}"))
    top = _r(b, "DFEDTARU")
    if top and top.changes.get("week") and abs(top.changes["week"]) >= 0.01:
        signals.append(("watch", f"Fed {'hiked' if top.changes['week'] > 0 else 'cut'} "
                                 f"{abs(top.changes['week']):.2f} pts this week"))
    for r in sorted(rows, key=lambda r: {"risk": 0, "watch": 1}.get(r.tone, 2)):
        if r.tone in ("risk", "watch"):
            signals.append((r.tone, f"{r.name}: {r.signal}"))
    dw = _r(b, "WLCFLPCL")
    if dw and (dw.changes.get("week") or 0) >= 3 and dw.bases.get("week") and dw.changes["week"] / dw.bases["week"] >= 0.5:
        signals.append(("watch", f"Discount window borrowing jumped ${dw.changes['week']:,.0f}B in a week"))

    ytd = None if b.failures.error else sum(1 for f in b.failures.failures if f.fail_date.year == b.today.year)
    sources = [(ITEMS[k].name, sources_for(k)) for _, keys in SUMMARY_GROUPS for k in keys]
    sources.append(("Bank failures", [("FDIC failed bank list", FDIC_FAILURES_URL)]))
    through = max((r.as_of for k, r in b.readings.items() if r.ok and r.as_of and k in ("DGS10", "EFFR", "SOFR")),
                  default=None)
    status = (f"{counts['risk']} risk · {counts['watch']} watch" if counts["risk"]
              else f"{counts['watch']} watch" if counts["watch"] else "all normal")
    missing = [r.name for r in rows if not r.loaded]
    missing += [lbl for lbl, err in (("bank failures", b.failures.error), ("calendar", b.calendar.error)) if err]
    rep = Report(b.today, through, f"{NAME} · {b.today:%a %b %-d} · {status}", counts, signals, groups, rates,
                 rates_panels, pages, last_panels, ytd, recent, b.calendar.events[:5], sources, b.sample, missing)
    from .narrative import write_summary
    rep.summary, rep.summary_by = write_summary(rep)
    return rep


# --------------------------------------------------------------------- email
def _badge(tone: str) -> str:
    return (f'<span style="display:inline-block;padding:1px 7px;border-radius:3px;background:{TONE_COLOR[tone]};'
            f'color:#fff;font-size:10px;font-weight:700;letter-spacing:.06em;white-space:nowrap">{TONE_WORD[tone].upper()}</span>')


def _link(text: str, url: Optional[str], color: str = INK) -> str:
    if not url:
        return _esc(text)
    return f'<a href="{_esc(url)}" style="color:{color};text-decoration:none;border-bottom:1px solid {RULE}">{_esc(text)}</a>'


def _h(title: str, page_no: int) -> str:
    return (f'<tr><td style="padding:30px 0 10px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr><td style="font-size:18px;font-weight:700;color:{INK};letter-spacing:-.01em">{_esc(title)}</td>'
            f'<td align="right" style="font-size:11px;color:{MUTED}">{page_no} / 5</td></tr></table>'
            f'<div style="height:2px;background:{INK};margin-top:6px"></div></td></tr>')


def _panel_html(p: Panel, image_src: Callable[[str], str]) -> str:
    img = (f'<img src="{image_src(p.cid)}" width="300" alt="{_esc(p.title)}" '
           f'style="display:block;width:100%;max-width:300px;height:auto;border:0;margin-top:6px">' if p.png else
           f'<p style="font-size:12px;color:{MUTED}">Chart unavailable</p>')
    return (f'<td valign="top" width="50%" style="padding:10px;background:{PANEL_BG};border:1px solid {RULE}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td style="font-size:12.5px;font-weight:700;color:{INK}">{_esc(p.title)}</td>'
            f'<td align="right">{_badge(p.tone)}</td></tr></table>'
            f'<div style="font-size:22px;font-weight:700;color:{INK};margin-top:4px;letter-spacing:-.01em">{_link(p.reading, p.url)}</div>'
            f'<div style="font-size:11.5px;color:{MUTED};margin-top:2px;line-height:1.35">{_esc(p.signal)}</div>{img}'
            + (f'<div style="font-size:10.5px;color:{MUTED};margin-top:4px">Source: '
               + " · ".join(_link(lbl, u, LINK) for lbl, u in p.sources) + "</div>" if p.sources else "")
            + "</td>")


def _grid(panels: list[Panel], image_src: Callable[[str], str]) -> str:
    rows = ""
    for i in range(0, len(panels), 2):
        pair = panels[i:i + 2]
        cells = '<td width="12" style="font-size:0">&nbsp;</td>'.join(_panel_html(p, image_src) for p in pair)
        if len(pair) == 1:
            cells += '<td width="12" style="font-size:0">&nbsp;</td><td width="50%"></td>'
        rows += f'<tr>{cells}</tr><tr><td colspan="3" style="height:12px;font-size:0">&nbsp;</td></tr>'
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>'


def _tile(label: str, n: int, tone: str) -> str:
    return (f'<td width="33%" style="padding:12px 14px;background:{PANEL_BG};border:1px solid {RULE};border-top:4px solid {TONE_COLOR[tone]}">'
            f'<div style="font-size:28px;font-weight:700;color:{INK};line-height:1">{n}</div>'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:.08em;color:{MUTED};margin-top:4px">{label}</div></td>')


def render_html(rep: Report, b: Bundle, image_src: Callable[[str], str] = lambda cid: f"cid:{cid}") -> str:
    th = f'style="padding:6px 6px;font-size:10px;font-weight:700;letter-spacing:.06em;color:{MUTED};text-align:right;border-bottom:1px solid {INK}"'
    td = f'style="padding:7px 6px;font-size:12px;color:{MUTED};text-align:right;border-bottom:1px solid {RULE};white-space:nowrap"'
    table = (f'<tr><th {th.replace("text-align:right", "text-align:left")}>MEASURE</th><th {th}>LATEST</th>'
             f'<th {th}>LAST</th><th {th}>1 MO</th><th {th}>1 YR</th><th {th}>STATUS</th></tr>')
    for title, rows in rep.groups:
        table += (f'<tr><td colspan="6" style="padding:12px 6px 4px;font-size:10.5px;font-weight:700;letter-spacing:.08em;'
                  f'color:{INK}">{_esc(title.upper())}</td></tr>')
        for r in rows:
            table += (f'<tr><td style="padding:7px 6px;border-bottom:1px solid {RULE};font-size:12.5px;color:{INK}">'
                      f'<b>{_esc(r.name)}</b><div style="font-size:11px;color:{MUTED};margin-top:1px">{_esc(r.signal)}</div></td>'
                      f'<td {td.replace(MUTED, INK)}><b>{_link(r.reading, r.url)}</b></td><td {td}>{_esc(r.last)}</td>'
                      f'<td {td}>{_esc(r.month)}</td><td {td}>{_esc(r.year)}</td><td {td}>{_badge(r.tone)}</td></tr>')

    color = {"risk": RISK_C, "watch": WATCH_C}
    sig = "".join(f'<tr><td width="10" valign="top" style="padding:5px 8px 5px 0"><div style="width:8px;height:8px;'
                  f'border-radius:4px;background:{color[t]};margin-top:5px"></div></td>'
                  f'<td style="padding:5px 0;font-size:13px;color:{INK}">{_esc(x)}</td></tr>' for t, x in rep.signals[:6])
    if not sig:
        sig = (f'<tr><td style="padding:5px 0;font-size:13px;color:{OK_C}"><b>No measures in a Watch or Risk range.</b></td></tr>')

    body = _h("Summary", 1)
    body += (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
             f'{_tile("RISK", rep.counts["risk"], "risk")}<td width="10"></td>{_tile("WATCH", rep.counts["watch"], "watch")}'
             f'<td width="10"></td>{_tile("NORMAL", rep.counts["ok"], "ok")}</tr></table></td></tr>')
    if rep.summary:
        by = (f'<div style="font-size:10.5px;color:{MUTED};margin-top:6px">Written by Claude from the figures in this report.</div>'
              if rep.summary_by == "Claude" else "")
        body += (f'<tr><td style="padding:16px 0 0"><div style="background:#f5f6f8;border-left:3px solid {INK};padding:10px 12px">'
                 f'<div style="font-size:10.5px;font-weight:700;letter-spacing:.08em;color:{MUTED}">WHAT\'S GOING ON</div>'
                 f'<div style="font-size:13.5px;line-height:1.5;color:{INK};margin-top:4px">{_esc(rep.summary)}</div>{by}</div></td></tr>')
    body += (f'<tr><td style="padding:16px 0 4px;font-size:10.5px;font-weight:700;letter-spacing:.08em;color:{MUTED}">KEY SIGNALS</td></tr>'
             f'<tr><td><table role="presentation" cellpadding="0" cellspacing="0">{sig}</table></td></tr>')
    body += f'<tr><td style="padding-top:14px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{table}</table></td></tr>'

    # page 2: rates table + two charts
    rth = f'style="padding:5px 6px;font-size:10px;font-weight:700;letter-spacing:.06em;color:{MUTED};text-align:right;border-bottom:1px solid {INK}"'
    rtd = f'style="padding:5px 6px;font-size:12px;color:{MUTED};text-align:right;border-bottom:1px solid {RULE};white-space:nowrap"'
    rt = (f'<tr><th {rth.replace("text-align:right", "text-align:left")}>RATE</th><th {rth}>LATEST</th><th {rth}>1 DAY</th>'
          f'<th {rth}>1 WK</th><th {rth}>1 MO</th><th {rth}>1 YR</th><th {rth}>AS OF</th></tr>')
    for title, rrows in rep.rates:
        rt += (f'<tr><td colspan="7" style="padding:10px 6px 3px;font-size:10.5px;font-weight:700;letter-spacing:.08em;'
               f'color:{INK}">{_esc(title.upper())}</td></tr>')
        for r in rrows:
            rt += (f'<tr><td style="padding:5px 6px;border-bottom:1px solid {RULE};font-size:12.5px;color:{INK}">{_esc(r.name)}</td>'
                   f'<td {rtd.replace(MUTED, INK)}><b>{_link(r.latest, r.url)}</b></td><td {rtd}>{_esc(r.day)}</td>'
                   f'<td {rtd}>{_esc(r.week)}</td><td {rtd}>{_esc(r.month)}</td><td {rtd}>{_esc(r.year)}</td>'
                   f'<td {rtd}>{_esc(r.as_of)}</td></tr>')
    body += _h("Rates", 2)
    body += f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rt}</table></td></tr>'
    body += f'<tr><td style="padding-top:14px">{_grid(rep.rates_panels, image_src)}</td></tr>'

    for i, (title, panels) in enumerate(rep.pages, start=3):
        body += _h(title, i) + f"<tr><td>{_grid(panels, image_src)}</td></tr>"

    body += _h("Markets & calendar", 5)
    fails = (f'<div style="font-size:22px;font-weight:700;color:{INK};margin-top:4px">{rep.failures_ytd if rep.failures_ytd is not None else "n/a"}</div>'
             f'<div style="font-size:11.5px;color:{MUTED}">failures in {rep.today.year}</div>')
    if rep.recent_failures:
        fails += "".join(f'<div style="font-size:12px;color:{INK};margin-top:6px"><b>{f.fail_date:%b %-d}</b> · {_esc(f.name)}, '
                         f'{_esc(f.city_state)}{deposits_note(f)}</div>' for f in rep.recent_failures[:8])
    else:
        fails += f'<div style="font-size:12px;color:{MUTED};margin-top:6px">None so far this year</div>'
    fails += f'<div style="margin-top:8px;font-size:11px">{_link("FDIC failed bank list", FDIC_FAILURES_URL, LINK)}</div>'
    fail_cell = (f'<td valign="top" width="50%" style="padding:10px;background:{PANEL_BG};border:1px solid {RULE}">'
                 f'<div style="font-size:12.5px;font-weight:700;color:{INK}">Bank failures</div>{fails}</td>')
    body += f"<tr><td>{_grid(rep.last_panels[:2], image_src)}</td></tr>"
    body += (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
             f'{_panel_html(rep.last_panels[2], image_src)}<td width="12"></td>{fail_cell}</tr></table></td></tr>')
    if rep.coming_up:
        ev = "".join(f'<tr><td style="padding:5px 12px 5px 0;font-size:12.5px;font-weight:700;color:{INK};white-space:nowrap">'
                     f'{e.when:%a %b %-d}</td><td style="padding:5px 0;font-size:12.5px;color:{INK}">{_esc(e.label)}</td></tr>'
                     for e in rep.coming_up)
        body += (f'<tr><td style="padding:18px 0 4px;font-size:10.5px;font-weight:700;letter-spacing:.08em;color:{MUTED}">COMING UP</td></tr>'
                 f'<tr><td><table role="presentation" cellpadding="0" cellspacing="0">{ev}</table></td></tr>')
    body += (f'<tr><td style="padding:18px 0 0;font-size:11px;color:{MUTED}"><b style="color:{INK}">Sources:</b> '
             f'{_link("FRED (St. Louis Fed)", "https://fred.stlouisfed.org", LINK)} · '
             f'{_link("FDIC", FDIC_FAILURES_URL, LINK)} · {_link("DeFiLlama", "https://defillama.com/stablecoins", LINK)}. '
             "Each reading links to its series.</td></tr>")
    if rep.missing:
        body += f'<tr><td style="padding-top:12px;font-size:11px;color:{MUTED}">Unavailable today: {_esc(", ".join(rep.missing))}</td></tr>'

    banner = ""
    if rep.sample:
        banner = ('<div style="background:#fff4ce;color:#5c4400;padding:8px 16px;font:13px sans-serif;text-align:center">'
                  "SAMPLE DATA · NOT REAL RATES</div>")
    through = f" · Data through {rep.data_through:%b %-d}" if rep.data_through else ""
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:{PAGE_BG}">{banner}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG}"><tr><td align="center" style="padding:24px 10px">
<table role="presentation" width="660" cellpadding="0" cellspacing="0" style="max-width:660px;width:100%;background:#fff;font-family:{FONT};padding:0">
<tr><td style="padding:26px 28px 6px;background:{INK}">
  <div style="font-size:11px;font-weight:700;letter-spacing:.12em;color:#9aa3ad">DAILY</div>
  <div style="font-size:22px;font-weight:700;color:#fff;margin-top:2px;letter-spacing:-.01em">{NAME}</div>
  <div style="font-size:12.5px;color:#c8cdd3;margin:4px 0 18px">{rep.today:%A, %B %-d, %Y}{through} · PDF attached</div>
</td></tr>
<tr><td style="padding:0 28px 28px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table></td></tr>
</table></td></tr></table></body></html>"""


def render_text(rep: Report) -> str:
    out = []
    if rep.sample:
        out.append("SAMPLE DATA - NOT REAL RATES\n")
    out += [f"{NAME.upper()} - {rep.today:%A, %B %-d, %Y}", "",
            f"Risk {rep.counts['risk']} | Watch {rep.counts['watch']} | Normal {rep.counts['ok']}", ""]
    if rep.summary:
        out += ["WHAT'S GOING ON", rep.summary, ""]
    out += [f"* {TONE_WORD[t]}: {x}" for t, x in rep.signals[:6]] or ["No measures in a Watch or Risk range."]
    for title, rows in rep.groups:
        out += ["", title.upper()]
        out += [f"  [{TONE_WORD[r.tone]:6}] {r.name}: {r.reading}  ({r.signal})" for r in rows]
    if rep.failures_ytd is not None:
        out += ["", f"Bank failures in {rep.today.year}: {rep.failures_ytd}"]
    if rep.coming_up:
        out += ["", "COMING UP"] + [f"  {e.when:%a %b %-d}: {e.label}" for e in rep.coming_up]
    out += ["", "Charts and full layout are in the HTML email and the attached PDF."]
    return "\n".join(out)
