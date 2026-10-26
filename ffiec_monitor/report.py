"""The daily email's content model and HTML rendering (the PDF, ffiec_monitor/pdf_report.py,
renders the same Report).

Layout, every section omitted when empty (same order as spec §7):
  header      name, date, status line
  summary     counts (Tier 1, Tier 2, Watch List changes, refilings, new filings) and a
              plain-English "What's going on" paragraph (ffiec_monitor/narrative.py)
  Watch List changes
  Tier 1 alerts            one card per institution: triggers + before/after ratio table
  Watch List snapshot      current ratios for every Watch List bank
  Tier 2 digest
  Refilings                no material capital change
  New quarterly filings    no threshold trigger
  sources

Long lists (refilings, new filings) show the largest institutions first, capped at
LIST_CAP rows; everything is in the attached CSV. Every institution links to its
Call Report on FFIEC CDR and its FDIC BankFind profile.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

NAME = "FFIEC Call Report Monitor"
LIST_CAP = 40

INK, MUTED, RULE, SOFT, LINK = "#14181f", "#5b626c", "#dde0e4", "#f5f6f8", "#1d5fbf"
RED, AMBER, GREEN, GRAY = "#b42318", "#8a5a00", "#1d6b3a", "#4b5563"

SCREEN_NOTE = ("New filings with at least one red flag for a bank that takes reciprocal or brokered deposits: "
               "capital near the well-capitalized line, losses, tight liquidity, or heavy brokered/reciprocal "
               "funding. Red = review now, amber = note it.")

CDR_HOME = "https://cdr.ffiec.gov/public/"
BANKFIND_HOME = "https://banks.data.fdic.gov/bankfind-suite/bankfind"


def bankfind_url(fdic_cert: Optional[str]) -> Optional[str]:
    return f"{BANKFIND_HOME}?cert={fdic_cert}" if fdic_cert else None


def _ubpr(rssd, period) -> Optional[str]:
    from .ubpr import ubpr_url
    return ubpr_url(str(rssd).strip(), period) if period else None


def quarter(period_end: date) -> str:
    return f"Q{(period_end.month - 1) // 3 + 1} {period_end.year}"


def fmt_assets(total_assets: Optional[float]) -> str:
    if total_assets is None:
        return "n/a"
    dollars = total_assets * 1000                       # Call Report dollars are in thousands
    if dollars >= 1e9:
        return f"${dollars / 1e9:,.1f}B"
    return f"${dollars / 1e6:,.0f}M"


def fmt_pct(v: Optional[float]) -> str:
    return f"{v:.2f}%" if v is not None else "n/a"


def fmt_change(old: Optional[float], new: Optional[float]) -> str:
    if old is None or new is None:
        return ""
    d = new - old
    return f"{'+' if d >= 0 else '-'}{abs(d):.2f} pts"


@dataclass
class Bank:
    name: str
    location: str
    rssd: str
    cert: str
    assets: str
    assets_raw: float
    period: str
    call_report_url: Optional[str]
    bankfind_url: Optional[str]
    ubpr_url: Optional[str] = None


@dataclass
class RatioRow:
    label: str
    before: str
    after: str
    change: str
    peer: str = ""
    sub: bool = False                                   # nested row (capital growth under loan growth)


@dataclass
class Alert:
    bank: Bank
    triggers: list[str]
    ratios: list[RatioRow]
    on_watch_list: bool = False
    snapshot: list[RatioRow] = field(default_factory=list)


@dataclass
class ListRow:
    bank: Bank
    detail: str
    flags: list = field(default_factory=list)      # screen.Flag, new filings only


@dataclass
class WatchChange:
    verb: str                                           # Added | Removed | Reason changed
    bank: Bank
    reason: str


@dataclass
class Report:
    run_date: date
    subject: str
    status: str
    counts: dict[str, int]
    watch_changes: list[WatchChange]
    tier1: list[Alert]
    snapshot: list[Alert]
    tier2: list[Alert]
    refilings: list[ListRow]
    refilings_total: int
    new_filings: list[ListRow]          # new filings with nothing flagged
    new_filings_total: int
    flagged: list[ListRow] = field(default_factory=list)   # new filings the screen flagged, worst first
    flagged_total: int = 0
    summary: str = ""
    summary_by: str = ""
    analytics: object = None                     # analytics.Analytics, when a DB was available
    flag_counts: list = field(default_factory=list)
    images: dict = field(default_factory=dict)   # cid -> PNG (charts)
    capital_checked: bool = False                # were any ratios available for the capital check
    ltwc: list = field(default_factory=list)     # (Bank, [reasons]) below a PCA well-capitalized minimum
    cblr: list = field(default_factory=list)     # (Bank, [reason]) CBLR banks below the 9% leverage line
    enforcement: list = field(default_factory=list)          # enforcement.Match
    enforcement_sources: object = None                       # enforcement.Sources, when checked
    edition: str = ""                                        # e.g. "Week ending Sun Oct 25" (default: the date)
    performance: list = field(default_factory=list)          # performance.BankPerformance for focus banks
    enforcement_notes: dict = field(default_factory=dict)    # rssd -> (discussion paragraph, 'Claude'|'rules')


# ------------------------------------------------------------------ building
def _bank(meta, period_end: Optional[date]) -> Bank:
    from .email_report import cdr_report_url
    period = period_end or meta.as_of_period
    return Bank(
        # FFIEC pads names/cities to fixed width; strip so tables and matching aren't thrown off.
        name=" ".join((meta.name or "").split()),
        location=f"{(meta.city or '').strip()}, {(meta.state or '').strip()}".strip(", "), rssd=str(meta.id_rssd).strip(),
        cert=str(meta.fdic_cert or "").strip(), assets=fmt_assets(meta.total_assets), assets_raw=meta.total_assets or 0,
        period=quarter(period) if period else "n/a",
        call_report_url=cdr_report_url(meta.id_rssd, period) if period else None,
        bankfind_url=bankfind_url(meta.fdic_cert),
        ubpr_url=_ubpr(meta.id_rssd, period),
    )


def _ratio_rows(changes) -> list[RatioRow]:
    rows = []
    for c in changes:
        rows.append(RatioRow(c.label, fmt_pct(c.old_value) if c.old_value is not None else "",
                             fmt_pct(c.new_value), fmt_change(c.old_value, c.new_value),
                             fmt_pct(c.peer_value) if c.peer_value is not None else ""))
        if c.paired is not None:
            p = c.paired
            rows.append(RatioRow(p.label, fmt_pct(p.old_value) if p.old_value is not None else "",
                                 fmt_pct(p.new_value), fmt_change(p.old_value, p.new_value), sub=True))
    return rows


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# Prompt Corrective Action "well capitalized" minimums (12 CFR 324.403), and the Community
# Bank Leverage Ratio framework: an electing bank reports only the leverage ratio and is
# deemed well capitalized above 9%.
WELL_CAP = [("tier1_leverage_ratio", 5.0, "Leverage ratio"), ("tier1_rbc_ratio", 8.0, "Tier 1 risk-based"),
            ("total_rbc_ratio", 10.0, "Total risk-based"), ("cet1_ratio", 6.5, "CET1")]
CBLR_MIN = 9.0


def capital_status(r: Optional[dict]) -> list[str]:
    """Reasons a filing is below a well-capitalized line; [] if it isn't (or no data)."""
    if not r:
        return []
    def v(k):
        x = r.get(k)
        return float(x) if isinstance(x, (int, float)) else None
    return [f"{label} {v(k):.2f}% (well capitalized: {m:g}%+)" for k, m, label in WELL_CAP
            if v(k) is not None and v(k) < m]


def cblr_status(r: Optional[dict]) -> Optional[str]:
    """A community bank leverage ratio (CBLR) bank -- reports only the leverage ratio -- below 9%.
    Not less than well capitalized by itself: 8-9% gets a two-quarter grace period."""
    if not r:
        return None
    lev, t1, tot = (r.get(k) for k in ("tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio"))
    if isinstance(lev, (int, float)) and t1 is None and tot is None and 5.0 <= lev < CBLR_MIN:
        grace = "grace period (8% to 9%)" if lev >= 8 else "below 8%: leaves the framework now"
        return f"Leverage ratio {lev:.2f}% vs. the 9% CBLR line: {grace}"
    return None


def build_report(watch_list_changes, tier1_events, watch_list_snapshot, tier2_events,
                 restatement_events, new_filing_events, run_date: date, analytics=None,
                 enforcement_sources=None, edition: str = "", prior_lookup=None) -> Report:
    watch = []
    for e in watch_list_changes:
        verb = {"ADDED": "Added", "REMOVED": "Removed"}.get(e.change.change_type, "Reason changed")
        watch.append(WatchChange(verb, _bank(e.meta, None), e.change.reason or e.change.previous_reason or "n/a"))

    tier1 = []
    for e in tier1_events:
        tier1.append(Alert(
            _bank(e.meta, e.period_end),
            [t.description + (" (escalated: Watch List bank)" if t.escalated else "") for t in e.triggers],
            _ratio_rows([c for t in e.triggers for c in t.ratio_changes]),
            e.on_watch_list,
            _ratio_rows(e.full_snapshot) if e.on_watch_list and e.full_snapshot else [],
        ))
    snapshot = [Alert(_bank(s.meta, None), [], _ratio_rows(s.ratios)) for s in watch_list_snapshot]
    tier2 = [Alert(_bank(e.meta, None), [t.description for t in e.triggers], []) for e in tier2_events]

    def rows(events, detail):
        out = [ListRow(_bank(e.meta, e.period_end), detail(e)) for e in events]
        out.sort(key=lambda r: r.bank.assets_raw, reverse=True)       # largest institutions first
        return out

    refilings = rows(restatement_events, lambda e: e.reason)
    from .screen import screen, severity
    new_all = [ListRow(_bank(e.meta, e.period_end), "Filed", screen(getattr(e, "ratios", None)))
               for e in new_filing_events]
    new_all.sort(key=lambda r: r.bank.assets_raw, reverse=True)
    # "To review" = at least one red (concern) flag; amber-only banks stay in the plain list (flags in the CSV).
    flagged = sorted((r for r in new_all if severity(r.flags)[0]), key=lambda r: (severity(r.flags), r.bank.assets_raw),
                     reverse=True)
    new_filings = [r for r in new_all if not severity(r.flags)[0]]

    counts = {"tier1": len(tier1), "tier2": len(tier2), "watch": len(watch),
              "refilings": len(refilings), "new": len(new_all), "flagged": len(flagged)}
    if tier1:
        status = _plural(len(tier1), "Tier 1 alert", "Tier 1 alerts")
    elif watch:
        status = "Watch List changed"
    elif tier2:
        status = _plural(len(tier2), "Tier 2 item", "Tier 2 items")
    else:
        bits = []
        if refilings:
            bits.append(_plural(len(refilings), "refiling", "refilings"))
        if new_all:
            bits.append(_plural(len(new_all), "new filing", "new filings")
                        + (f" ({len(flagged)} flagged)" if flagged else ""))
        status = ", ".join(bits) or "no activity"
    # Capital check across every filing that has ratios (new filings and pulled refilings).
    ltwc, cblr, checked = [], [], False
    for e in list(new_filing_events) + list(restatement_events):
        r = getattr(e, "ratios", None)
        checked = checked or bool(r)
        reasons = capital_status(r)
        if reasons:
            ltwc.append((_bank(e.meta, e.period_end), reasons))
        elif cblr_status(r):
            cblr.append((_bank(e.meta, e.period_end), [cblr_status(r)]))
    ltwc.sort(key=lambda t: t[0].assets_raw, reverse=True)
    cblr.sort(key=lambda t: t[0].assets_raw, reverse=True)
    if checked:
        counts["ltwc"] = len(ltwc)
        counts["cblr"] = len(cblr)
    matches = []
    if enforcement_sources is not None:
        from .enforcement import match
        all_banks = ([r.bank for r in refilings] + [r.bank for r in new_all] + [a.bank for a in tier1]
                     + [a.bank for a in tier2] + [a.bank for a in snapshot] + [w.bank for w in watch])
        uniq = list({b.rssd: b for b in all_banks}.values())
        matches = match(uniq, enforcement_sources.actions)
        # Orders 10+ years old with no termination in the data are almost always closed: footnoted, not counted.
        counts["enforcement"] = len({m.bank.rssd for m in matches if not m.action.stale})
    if ltwc:
        status = _plural(len(ltwc), "bank below well capitalized", "banks below well capitalized") + " · " + status
    if counts.get("enforcement"):
        status = _plural(counts["enforcement"], "bank with an active enforcement or AML action",
                         "banks with active enforcement or AML actions") \
                 + " · " + status
    subject = f"{NAME} · {edition or f'{run_date:%a %b %-d}'} · {status}"
    rep = Report(run_date, subject, status, counts, watch, tier1, snapshot, tier2,
                 refilings[:LIST_CAP], len(refilings), new_filings[:LIST_CAP // 2], len(new_filings),
                 flagged[:LIST_CAP], len(flagged))
    rep.capital_checked, rep.ltwc, rep.cblr = checked, ltwc, cblr
    # Performance of the banks that matter most: under an order, below well capitalized, CBLR under 9%.
    current = {}
    for e in list(new_filing_events) + list(restatement_events):
        if getattr(e, "ratios", None):
            current[str(e.meta.id_rssd).strip()] = (e.ratios, e.period_end)
    focus: dict[str, tuple] = {}
    def add(b, why):
        if b.rssd in current:
            entry = focus.setdefault(b.rssd, (b, [], *current[b.rssd]))
            if why not in entry[1]:
                entry[1].append(why)
    for m in matches:
        if not m.action.stale:
            add(m.bank, f"{m.action.agency} {m.action.action.split(',')[0].lower()}")
    for b_, _rs in ltwc:
        add(b_, "below well capitalized")
    for b_, _rs in cblr:
        add(b_, "CBLR bank under 9%")
    if focus:
        from .performance import build as build_perf
        rep.performance = build_perf(list(focus.values()), prior_lookup)
    rep.enforcement, rep.enforcement_sources, rep.edition = matches, enforcement_sources, edition
    if matches:
        from .narrative import enforcement_notes
        rep.enforcement_notes = enforcement_notes(rep)
    from . import analytics as an
    rep.analytics = analytics
    rep.flag_counts = an.flag_counts(new_all)
    try:
        for cid, png in (("activity", an.activity_chart(analytics) if analytics else None),
                         ("flags", an.flags_chart(rep.flag_counts))):
            if png:
                rep.images[cid] = png
    except Exception:                                  # charts are optional; never block the email
        pass
    from .narrative import write_summary
    rep.summary, rep.summary_by = write_summary(rep)
    return rep


# ------------------------------------------------------------------ plain text (fed to the summary writer)
def facts(rep: Report, limit: int = 40) -> str:
    c = rep.counts
    out = [f"{NAME} daily run {rep.run_date:%B %-d, %Y}",
           f"Tier 1 alerts: {c['tier1']}. Tier 2 items: {c['tier2']}. Watch List changes: {c['watch']}. "
           f"Refilings with no material capital change: {c['refilings']}. New quarterly filings with no "
           f"threshold trigger: {c['new']}, of which {c['flagged']} flagged by the level screen for capital, earnings, "
           f"liquidity or brokered/reciprocal deposit concerns. Watch List banks: {len(rep.snapshot)}."]
    a = rep.analytics
    if a is not None and a.period_end and a.universe:
        out.append(f"{a.quarter} filing season: {a.filed:,} of {a.universe:,} banks have filed ({a.pct:.0f}%); "
                   f"deadline {a.deadline:%B %-d}.")
    if rep.flag_counts:
        out.append("Flag types among today's new filings (banks with a red flag / amber only): "
                   + "; ".join(f"{l} {r}/{w}" for l, r, w in rep.flag_counts))
    if rep.capital_checked:
        out.append(f"LESS THAN WELL CAPITALIZED (PCA minimums): {len(rep.ltwc)} filings.")
        out += [f"  {b_.name} ({b_.location}), assets {b_.assets}, {b_.period}: " + "; ".join(rs) for b_, rs in rep.ltwc[:limit]]
        out.append(f"CBLR BANKS UNDER THE 9% LEVERAGE LINE (not less than well capitalized by themselves): {len(rep.cblr)}.")
        out += [f"  {b_.name} ({b_.location}), assets {b_.assets}: {rs[0]}" for b_, rs in rep.cblr[:limit]]
    if rep.enforcement_sources is not None:
        out.append(f"ENFORCEMENT / AML: {rep.counts.get('enforcement', 0)} banks with an active order, agreement or "
                   f"recent FinCEN action. " + coverage_note(rep))
        out += [f"  {m.bank.name} ({m.bank.location}): {m.action.agency} {m.action.topic}: {m.action.action}"
                + (" (over 10 years old with no termination in the data: most likely closed, NOT counted)" if m.action.stale else "")
                + (f" since {m.action.start:%B %-d, %Y}" if m.action.start else "") for m in rep.enforcement[:limit]]
    if rep.performance:
        out.append("PERFORMANCE OF FLAGGED BANKS (this quarter vs. last):")
        for bp in rep.performance:
            vals = "; ".join(f"{r.label} {'n/a' if r.prior is None else f'{r.prior:.2f}%'} to "
                             f"{'n/a' if r.current is None else f'{r.current:.2f}%'}" for r in bp.rows
                             if r.current is not None)
            out.append(f"  {bp.bank.name} ({bp.bank.location}, {bp.bank.assets}; {', '.join(bp.why)}): {bp.read} {vals}")
    for w in rep.watch_changes:
        out.append(f"WATCH LIST {w.verb.upper()}: {w.bank.name} ({w.bank.location}), reason: {w.reason}")
    for a in rep.tier1:
        out.append(f"TIER 1: {a.bank.name} ({a.bank.location}), assets {a.bank.assets}, {a.bank.period}"
                   + (", on Watch List" if a.on_watch_list else ""))
        out += [f"  trigger: {t}" for t in a.triggers]
        out += [f"  {r.label}: {r.before or 'n/a'} to {r.after} {r.change}".rstrip() for r in a.ratios]
    for a in rep.tier2:
        out.append(f"TIER 2: {a.bank.name} ({a.bank.location}), assets {a.bank.assets}: " + "; ".join(a.triggers))
    if rep.refilings:
        out.append(f"REFILINGS (largest first, {min(limit, len(rep.refilings))} of {rep.refilings_total}):")
        out += [f"  {r.bank.name} ({r.bank.location}), assets {r.bank.assets}, {r.bank.period}: {r.detail}"
                for r in rep.refilings[:limit]]
    if rep.flagged:
        out.append(f"NEW FILINGS FLAGGED (worst first, {min(limit, len(rep.flagged))} of {rep.flagged_total}):")
        for r in rep.flagged[:limit]:
            out.append(f"  {r.bank.name} ({r.bank.location}), assets {r.bank.assets}, {r.bank.period}: "
                       + "; ".join(f"{f.level}: {f.text}" for f in r.flags))
    if rep.new_filings:
        out.append(f"OTHER NEW FILINGS, nothing flagged (largest first, {min(limit, len(rep.new_filings))} of {rep.new_filings_total}):")
        out += [f"  {r.bank.name} ({r.bank.location}), assets {r.bank.assets}, {r.bank.period}"
                for r in rep.new_filings[:limit]]
    return "\n".join(out)


# ------------------------------------------------------------------ HTML
def _e(x) -> str:
    return html.escape(str(x))


def _a(text: str, url: Optional[str], color: str = LINK) -> str:
    return f'<a href="{_e(url)}" style="color:{color};text-decoration:underline">{_e(text)}</a>' if url else _e(text)


def _links(b: Bank) -> str:
    parts = []
    if b.call_report_url:
        parts.append(_a("Call Report", b.call_report_url))
    if b.ubpr_url:
        parts.append(_a("UBPR", b.ubpr_url))
    if b.bankfind_url:
        parts.append(_a("FDIC profile", b.bankfind_url))
    return " · ".join(parts)


def _section(title: str, note: str = "") -> str:
    n = f'<div style="font-size:14.2px;color:{MUTED};margin-top:3px">{_e(note)}</div>' if note else ""
    return (f'<tr><td style="padding:26px 0 8px;border-bottom:2px solid {INK}">'
            f'<div style="font-size:20.1px;font-weight:700;color:{INK}">{_e(title)}</div>{n}</td></tr>')


TH = f'style="padding:6px 8px;font-size:11.8px;font-weight:700;letter-spacing:.06em;color:{MUTED};text-align:left;border-bottom:1px solid {INK}"'
THR = TH.replace("text-align:left", "text-align:right")
TD = f'style="padding:6px 8px;font-size:14.8px;color:{INK};border-bottom:1px solid {RULE};vertical-align:top"'
TDR = TD.replace("vertical-align:top", "vertical-align:top;text-align:right;white-space:nowrap")
TDM = TD.replace(f"color:{INK}", f"color:{MUTED}")


def _bank_cell(b: Bank) -> str:
    return (f'<td {TD}><b>{_e(b.name)}</b><div style="font-size:13px;color:{MUTED}">{_e(b.location)} · '
            f'{_e(b.assets)}</div></td>')


def _ratio_table(rows: list[RatioRow], before_label: str = "BEFORE") -> str:
    peer = any(r.peer for r in rows)
    head = (f'<tr><th {TH}>RATIO</th><th {THR}>{before_label}</th><th {THR}>AFTER</th><th {THR}>CHANGE</th>'
            + (f'<th {THR}>PEER AVG</th>' if peer else "") + "</tr>")
    body = ""
    for r in rows:
        label = f'<span style="color:{MUTED}">&nbsp;&nbsp;↳ {_e(r.label)}</span>' if r.sub else _e(r.label)
        body += (f'<tr><td {TD}>{label}</td><td {TDR}>{_e(r.before)}</td><td {TDR}><b>{_e(r.after)}</b></td>'
                 f'<td {TDR}>{_e(r.change)}</td>' + (f'<td {TDR}>{_e(r.peer)}</td>' if peer else "") + "</tr>")
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{body}</table>'


def _current_table(rows: list[RatioRow]) -> str:
    body = "".join(f'<tr><td {TD}>{"&nbsp;&nbsp;↳ " if r.sub else ""}{_e(r.label)}</td><td {TDR}><b>{_e(r.after)}</b></td>'
                   f'<td {TDR}>{_e(r.peer)}</td></tr>' for r in rows)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><th {TH}>RATIO</th>'
            f'<th {THR}>CURRENT</th><th {THR}>PEER AVG</th></tr>{body}</table>')


def _tile(label: str, n: int, color: str) -> str:
    return (f'<td valign="top" style="border:1px solid {RULE};border-top:4px solid {color};padding:9px 10px">'
            f'<div style="font-size:26px;font-weight:700;color:{INK}">{n}</div>'
            f'<div style="font-size:11.8px;font-weight:700;letter-spacing:.06em;color:{MUTED}">{_e(label)}</div></td>')


def _list_table(rows: list[ListRow], total: int, detail_label: Optional[str]) -> str:
    head = (f'<tr><th {TH}>INSTITUTION</th><th {TH}>PERIOD</th>'
            + (f'<th {TH}>{detail_label}</th>' if detail_label else "") + f'<th {TH}>LINKS</th></tr>')
    body = "".join(f'<tr>{_bank_cell(r.bank)}<td {TDM}>{_e(r.bank.period)}</td>'
                   + (f'<td {TD}>{_e(r.detail)}</td>' if detail_label else "")
                   + f'<td {TD} nowrap>{_links(r.bank)}</td></tr>' for r in rows)
    more = (f'<tr><td colspan="4" style="padding:8px;font-size:14.2px;color:{MUTED}">Showing the {len(rows)} largest of '
            f'{total}. All {total} are in the attached CSV.</td></tr>' if total > len(rows) else "")
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{head}{body}{more}</table>'


def _progress_html(a) -> str:
    left = a.days_left
    due = (f" ({left} days left)" if left and left > 1 else " (tomorrow)" if left == 1 else " (today)" if left == 0
           else " (past due)" if left is not None and left < 0 else "")
    pct = min(a.pct, 100)
    return (f'<div style="font-size:12.4px;font-weight:700;letter-spacing:.08em;color:{MUTED}">{_e(a.quarter.upper())} FILING SEASON</div>'
            f'<div style="font-size:15.3px;color:{INK};margin:4px 0 6px"><b>{a.filed:,}</b> of {a.universe:,} banks have filed '
            f'({a.pct:.0f}%) · due {a.deadline:%a %b %-d}{due}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="height:10px"><tr>'
            f'<td width="{pct:.1f}%" style="background:#2a78d6;height:10px;font-size:0;line-height:0">&nbsp;</td>'
            f'<td style="background:#e6e8eb;height:10px;font-size:0;line-height:0">&nbsp;</td></tr></table>')


def _glance_html(rep: Report, image_src=None) -> str:
    """Progress bar + two charts under the count tiles."""
    src = image_src or (lambda cid: f"cid:{cid}")
    a = rep.analytics
    out = ""
    if a is not None and a.period_end and a.universe:
        out += f'<tr><td style="padding:16px 0 0">{_progress_html(a)}</td></tr>'
    for cid, title in (("activity", "Filings logged per day, last 21 days"),
                       ("flags", "Today's new filings: what was flagged")):
        if cid in rep.images:
            out += (f'<tr><td style="padding:20px 0 0"><div style="font-size:16.5px;font-weight:700;color:{INK};margin-bottom:6px">'
                    f'{_e(title)}</div><img src="{src(cid)}" width="712" alt="{_e(title)}" '
                    f'style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')
    return out


LTWC_NOTE = ("Filings below a Prompt Corrective Action well-capitalized minimum: leverage 5%, Tier 1 risk-based 8%, "
             "total risk-based 10%, CET1 6.5%. Below well capitalized, a bank faces limits on brokered deposits and on the "
             "reciprocal deposits it can exclude from them.")
CBLR_NOTE = ("These banks use the community bank leverage ratio (CBLR) framework: they report only a leverage ratio and are "
             "deemed well capitalized above 9%. Between 8% and 9% they get a two-quarter grace period, then must meet the "
             "risk-based rules or get back above 9%; below 8% they leave the framework right away. Not less than well "
             "capitalized by itself, but the cushion is thin.")


def coverage_note(rep: Report) -> str:
    src = rep.enforcement_sources
    if src is None:
        return ""
    parts = [f"{k} ({v:,} {'active' if k != 'FinCEN' else 'last 5 years'})" for k, v in src.loaded.items()]
    fdic = (f"FDIC orders come from the FDIC's order search as exported {src.fdic_as_of} (no live feed), matched by "
            "FDIC cert; FDIC consent orders cover safety and soundness and/or BSA/AML, so open the order for its scope."
            if "FDIC" in src.loaded else
            "FDIC orders aren't published as a data feed and no FDIC export is loaded, so they aren't checked.")
    return ("Active Fed, OCC and FDIC orders and agreements (cease and desist, written or formal agreements, consent "
            "orders, capital and PCA directives) and FinCEN Bank Secrecy Act actions from the last 3 years. One-time "
            "civil money penalties are left out. Checked against " + ", ".join(parts + [f"{k} unavailable today" for k in src.errors])
            + ". " + fdic + " Fed, OCC and FinCEN matches are by bank name and state; confirm with the source.")
    bad = [f"{k} unavailable today" for k in src.errors]
    return ("Checked against " + ", ".join(parts + bad) + ". Orders and agreements (cease and desist, written or formal "
            "agreements, consent orders, PCA directives) are listed first; civil money penalties are one-time fines, shown "
            "only from the last 3 years. FDIC orders aren't published as a data feed, so they aren't checked automatically. "
            "Matches are by bank name and state; confirm with the source.")


def _risk_sections_html(rep: Report) -> str:
    b = ""
    if rep.capital_checked:
        b += _section("Less than well capitalized", LTWC_NOTE)
        if rep.ltwc:
            rows = "".join(f'<tr>{_bank_cell(b_)}<td {TDM}>{_e(b_.period)}</td><td {TD}>{"<br>".join(_e(x) for x in rs)}</td>'
                           f'<td {TD} nowrap>{_links(b_)}</td></tr>' for b_, rs in rep.ltwc)
            b += (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><th {TH}>INSTITUTION</th>'
                  f'<th {TH}>PERIOD</th><th {TH}>BELOW THE LINE</th><th {TH}>LINKS</th></tr>{rows}</table></td></tr>')
        else:
            b += f'<tr><td style="padding:8px 0;font-size:15.3px;color:{INK}">None. Every filing with capital data is at or above the well-capitalized lines.</td></tr>'
        if rep.cblr:
            b += _section("Community bank leverage ratio banks under 9%", CBLR_NOTE)
            rows = "".join(f'<tr>{_bank_cell(b_)}<td {TDM}>{_e(b_.period)}</td><td {TD}>{_e(rs[0])}</td>'
                           f'<td {TD} nowrap>{_links(b_)}</td></tr>' for b_, rs in rep.cblr)
            b += (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><th {TH}>INSTITUTION</th>'
                  f'<th {TH}>PERIOD</th><th {TH}>LEVERAGE</th><th {TH}>LINKS</th></tr>{rows}</table></td></tr>')
    if rep.enforcement_sources is not None:
        active = [m for m in rep.enforcement if not m.action.stale]
        stale = [m for m in rep.enforcement if m.action.stale]
        b += _section("Banks under enforcement orders", coverage_note(rep))
        if active:
            from .narrative import RECIPROCAL_NOTE
            for m in active:
                note, by = rep.enforcement_notes.get(m.bank.rssd, ("", ""))
                b += (f'<tr><td style="padding-top:12px"><div style="border-left:3px solid {RED};padding:6px 0 6px 12px">'
                      f'<div style="font-size:16.5px;font-weight:700;color:{INK}">{_e(m.bank.name)}</div>'
                      f'<div style="font-size:14.2px;color:{MUTED}">{_e(m.bank.location)} · {_e(m.bank.assets)} · '
                      f'<b>{_e(m.action.agency)}</b> {_e(m.action.action)}'
                      f'{_e(f" · issued {m.action.start:%b %-d, %Y}" if m.action.start else "")} · {_e(m.action.topic)} · '
                      f'{_a("Order", m.action.url)} · {_links(m.bank)}</div>'
                      + (f'<div style="font-size:15.3px;line-height:1.5;color:{INK};margin-top:5px">{_e(note)}</div>' if note else "")
                      + "</div></td></tr>")
            b += f'<tr><td style="padding:10px 0 0;font-size:14.2px;color:{MUTED}">{_e(RECIPROCAL_NOTE)}</td></tr>'
        else:
            b += (f'<tr><td style="padding:8px 0;font-size:15.3px;color:{INK}">No bank in this report is under an active Fed, '
                  f'OCC or FDIC order or a recent FinCEN action.</td></tr>')
        if stale:
            b += (f'<tr><td style="padding:8px 0 0;font-size:14.2px;color:{MUTED}">Not counted: '
                  + "; ".join(f"{_e(m.bank.name)} ({_e(m.action.agency)} {_e(m.action.action.split(' (')[0])}, "
                              f"{m.action.start:%Y}) has no termination date in the data but is over 10 years old, so it "
                              f"is most likely closed. {_a('Check', m.action.url)}" for m in stale)
                  + "</td></tr>")
    return b


PERF_NOTE = ("Banks under an active enforcement order, below well capitalized, or a community bank leverage ratio bank "
             "under 9%: this quarter vs. last quarter. Green = moved the healthy way, red = the wrong way.")


def _performance_html(rep: Report) -> str:
    if not rep.performance:
        return ""
    b = _section("How the flagged banks are performing", PERF_NOTE)
    for bp in rep.performance:
        rows = ""
        for r in bp.rows:
            if r.current is None and r.prior is None:
                continue
            ch = "" if r.change is None else f"{'+' if r.change >= 0 else '-'}{abs(r.change):.2f}"
            color = GREEN if r.better is True else RED if r.better is False else MUTED
            rows += (f'<tr><td {TD}>{_e(r.label)}</td><td {TDR}>{"" if r.prior is None else f"{r.prior:.2f}%"}</td>'
                     f'<td {TDR}><b>{"" if r.current is None else f"{r.current:.2f}%"}</b></td>'
                     f'<td {TDR}><span style="color:{color}">{ch}</span></td></tr>')
        b += (f'<tr><td style="padding-top:14px"><div style="font-size:16.5px;font-weight:700;color:{INK}">{_e(bp.bank.name)}</div>'
              f'<div style="font-size:14.2px;color:{MUTED}">{_e(bp.bank.location)} · {_e(bp.bank.assets)} · '
              f'{_e("; ".join(bp.why))} · {_links(bp.bank)}</div>'
              f'<div style="font-size:15.3px;color:{INK};margin:5px 0">{_e(bp.read)}</div>'
              f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><th {TH}>RATIO</th>'
              f'<th {THR}>{_e(bp.prior_label.upper())}</th><th {THR}>{_e(bp.bank.period.upper())}</th><th {THR}>CHANGE</th></tr>'
              f'{rows}</table></td></tr>')
    return b


def tile_specs(c: dict) -> list[tuple[str, int, str]]:
    """Count tiles for the top of the email/PDF. Tier 1, Tier 2 and Watch List tiles only
    appear when they are non-zero; filings, refilings and flagged always show."""
    tiles = [("TIER 1 ALERTS", c["tier1"], RED), ("TIER 2", c["tier2"], AMBER), ("WATCH LIST", c["watch"], GRAY)]
    tiles = [t for t in tiles if t[1]]
    if "enforcement" in c:
        tiles.append(("ENFORCEMENT / AML", c["enforcement"], RED if c["enforcement"] else GRAY))
    if "ltwc" in c:
        tiles.append(("BELOW WELL CAP.", c["ltwc"], RED if c["ltwc"] else GRAY))
    if c.get("cblr"):
        tiles.append(("CBLR UNDER 9%", c["cblr"], AMBER))
    return tiles + [("NEW FILINGS", c["new"], GREEN), ("REFILINGS", c["refilings"], GRAY),
                    ("NEW, FLAGGED", c["flagged"], RED if c["flagged"] else GRAY)]


HIGHLIGHT_ROWS = 10        # rows per list in the email; the spreadsheet has everything


def _wrap(rep: Report, body: str) -> str:
    return (f'<!doctype html><html><body style="margin:0;background:#eef0f3;font-family:-apple-system,Segoe UI,Arial,sans-serif">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f3"><tr><td align="center" style="padding:18px 10px">'
            f'<table role="presentation" width="760" cellpadding="0" cellspacing="0" style="max-width:760px;width:100%;background:#fff">'
            f'<tr><td style="background:{INK};padding:18px 24px"><div style="font-size:13px;font-weight:700;letter-spacing:.12em;color:#9aa3ad">'
            f'{"WEEKLY" if rep.edition else "DAILY"}</div>'
            f'<div style="font-size:23.6px;font-weight:700;color:#fff">{NAME}</div>'
            f'<div style="font-size:14.8px;color:#c8cdd3;margin-top:3px">{_e(rep.edition) or f"{rep.run_date:%A, %B %-d, %Y}"} · '
            f'{_e(rep.status)}</div></td></tr>'
            f'<tr><td style="padding:18px 24px 26px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table></td></tr>'
            f'</table></td></tr></table></body></html>')


def _img_row(rep: Report, cid: str, title: str, src) -> str:
    if cid not in rep.images:
        return ""
    return (f'<tr><td style="padding:16px 0 0"><div style="font-size:15px;font-weight:700;color:{INK};margin-bottom:6px">{_e(title)}</div>'
            f'<img src="{src(cid)}" width="712" alt="{_e(title)}" style="display:block;width:100%;max-width:712px;height:auto;border:0"></td></tr>')


def _more_html(shown: int, total: int) -> str:
    return (f'<tr><td style="padding:6px 0 0;font-size:13px;color:{MUTED}">Showing {shown} of {total}. All {total} are in the '
            f'attached spreadsheet.</td></tr>' if total > shown else "")


def render_html(rep: Report, image_src=None) -> str:
    """The email: the same highlights as the five-page PDF, page by page. Full detail for every
    filing (ratios, flags, capital status, enforcement) is in the attached spreadsheet."""
    from .narrative import RECIPROCAL_NOTE
    src = image_src or (lambda cid: f"cid:{cid}")
    b = ('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
         + '<td width="8"></td>'.join(_tile(label, n, color) for label, n, color in tile_specs(rep.counts))
         + "</tr></table></td></tr>")
    a = rep.analytics
    if a is not None and a.period_end and a.universe:
        b += f'<tr><td style="padding:16px 0 0">{_progress_html(a)}</td></tr>'
    if rep.summary:
        b += (f'<tr><td style="padding:16px 0 0"><div style="background:{SOFT};border-left:3px solid {INK};padding:10px 12px">'
              f'<div style="font-size:12.4px;font-weight:700;letter-spacing:.08em;color:{MUTED}">WHAT\'S GOING ON</div>'
              f'<div style="font-size:15.9px;line-height:1.5;color:{INK};margin-top:4px">{_e(rep.summary)}</div></div></td></tr>')
    b += _img_row(rep, "activity", "Filings logged per day, last 21 days", src)

    # 2. Enforcement orders and capital
    if rep.enforcement_sources is not None or rep.capital_checked:
        src_ = rep.enforcement_sources
        short = ("Active Fed, OCC and FDIC orders and agreements and recent FinCEN BSA actions (no civil money penalties), "
                 "checked against " + ", ".join(f"{k} ({v:,})" for k, v in src_.loaded.items())
                 + (f"; unavailable today: {', '.join(src_.errors)}" if src_.errors else "")
                 + ". FDIC orders matched by FDIC cert, others by name and state." if src_ is not None else "")
        b += _section("Enforcement orders and capital", short)
        active = [m for m in rep.enforcement if not m.action.stale]
        for m in active:
            note, _by = rep.enforcement_notes.get(m.bank.rssd, ("", ""))
            issued = f" · issued {m.action.start:%b %-d, %Y}" if m.action.start else ""
            b += (f'<tr><td style="padding-top:12px"><div style="border-left:3px solid {RED};padding:4px 0 4px 12px">'
                  f'<div style="font-size:16px;font-weight:700;color:{INK}">{_e(m.bank.name)}'
                  f'<span style="font-weight:400;color:{MUTED};font-size:13.5px"> · {_e(m.bank.location)} · {_e(m.bank.assets)}</span></div>'
                  f'<div style="font-size:13.5px;color:{MUTED}"><b>{_e(m.action.agency)}</b> {_e(m.action.action)}{_e(issued)} · '
                  f'{_a("Order", m.action.url)} · {_links(m.bank)}</div>'
                  + (f'<div style="font-size:15px;line-height:1.5;color:{INK};margin-top:4px">{_e(note)}</div>' if note else "")
                  + "</div></td></tr>")
        if rep.enforcement_sources is not None:
            if active:
                b += f'<tr><td style="padding:8px 0 0;font-size:13px;color:{MUTED}">{_e(RECIPROCAL_NOTE)}</td></tr>'
            else:
                b += (f'<tr><td style="padding:8px 0;font-size:15px;color:{INK}">No bank is under an active Fed, OCC or FDIC order '
                      f'or a recent FinCEN action.</td></tr>')
            stale = [m for m in rep.enforcement if m.action.stale]
            if stale:
                b += (f'<tr><td style="padding:6px 0 0;font-size:13px;color:{MUTED}">Not counted: '
                      + "; ".join(f"{_e(m.bank.name)} ({_e(m.action.agency)}, {m.action.start:%Y}) has no termination date "
                                  f"but is over 10 years old, most likely closed. {_a('Check', m.action.url)}" for m in stale)
                      + "</td></tr>")
        if rep.capital_checked:
            if rep.ltwc:
                rows = "".join(f'<tr>{_bank_cell(b_)}<td {TD}>{_e(rs[0])}</td><td {TD} nowrap>{_links(b_)}</td></tr>'
                               for b_, rs in rep.ltwc[:HIGHLIGHT_ROWS])
                b += (f'<tr><td style="padding:14px 0 4px;font-size:15px;font-weight:700;color:{INK}">Less than well capitalized ({len(rep.ltwc)})</td></tr>'
                      f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>'
                      + _more_html(min(HIGHLIGHT_ROWS, len(rep.ltwc)), len(rep.ltwc)))
            else:
                b += f'<tr><td style="padding:12px 0 0;font-size:15px;color:{INK}"><b>Less than well capitalized:</b> none.</td></tr>'
            if rep.cblr:
                names = ", ".join(f"{_e(b_.name)} ({rs[0].split('%')[0].replace('Leverage ratio ', '')}%)" for b_, rs in rep.cblr)
                b += (f'<tr><td style="padding:8px 0 0;font-size:15px;color:{INK}"><b>Community bank leverage ratio banks under 9% '
                      f'({len(rep.cblr)}):</b> {names}. Still well capitalized during the two-quarter grace period.</td></tr>')

    # 3. Flagged-bank performance
    if rep.performance:
        keys = [("Leverage ratio", "LEVERAGE"), ("Return on assets", "ROA"), ("Noncurrent loans + OREO", "PROBLEM LOANS"),
                ("Brokered/reciprocal share", "BROK./RECIP.")]
        perf = sorted(rep.performance, key=lambda bp: ("Mostly worsening" not in bp.read, "order" not in " ".join(bp.why)))

        def cell(bp, label):
            r = next((x for x in bp.rows if x.label == label), None)
            if r is None or r.current is None:
                return f"<td {TDR}></td>"
            ch = ("" if r.change is None else
                  f'<div style="font-size:12px;color:{GREEN if r.better else RED if r.better is False else MUTED}">'
                  f'{"+" if r.change >= 0 else "-"}{abs(r.change):.2f}</div>')
            return f"<td {TDR}>{r.current:.2f}%{ch}</td>"
        rows = "".join(f'<tr><td {TD}><b>{_e(bp.bank.name)}</b><div style="font-size:12px;color:{MUTED}">{_e("; ".join(bp.why))} · '
                       f'{_e(bp.read.split(":")[0])}</div></td>' + "".join(cell(bp, k) for k, _ in keys) + "</tr>"
                       for bp in perf[:HIGHLIGHT_ROWS])
        b += _section("How the flagged banks are performing", f"This quarter vs. {perf[0].prior_label}; change underneath "
                      "(green = healthier, red = worse).")
        b += (f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><th {TH}>BANK</th>'
              + "".join(f"<th {THR}>{h}</th>" for _, h in keys) + f"</tr>{rows}</table></td></tr>"
              + _more_html(min(HIGHLIGHT_ROWS, len(perf)), len(perf)))

    # 4. New filings to review
    if rep.flagged or "flags" in rep.images:
        b += _section("New filings to review", SCREEN_NOTE)
        b += _img_row(rep, "flags", "Today's new filings: what was flagged", src)
        rows = "".join(
            f'<tr>{_bank_cell(r.bank)}<td {TD}>'
            + "".join(f'<div><span style="color:{RED if f.level == "concern" else AMBER}">●</span> {_e(f.text)}</div>'
                      for f in r.flags[:2])
            + f'</td><td {TD} nowrap>{_links(r.bank)}</td></tr>' for r in rep.flagged[:HIGHLIGHT_ROWS])
        if rows:
            b += f'<tr><td style="padding-top:8px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>'
        b += _more_html(min(HIGHLIGHT_ROWS, rep.flagged_total), rep.flagged_total)

    # 5. Alerts, refilings, other filings
    alerts = ([("Tier 1", a_.bank, "; ".join(a_.triggers[:2])) for a_ in rep.tier1]
              + [("Tier 2", a_.bank, "; ".join(a_.triggers[:2])) for a_ in rep.tier2]
              + [(f"Watch List {w.verb.lower()}", w.bank, w.reason) for w in rep.watch_changes])
    b += _section("Alerts, refilings and other filings")
    if alerts:
        rows = "".join(f'<tr><td {TD}><b>{_e(k)}</b></td>{_bank_cell(bk)}<td {TD}>{_e(t)}</td><td {TD} nowrap>{_links(bk)}</td></tr>'
                       for k, bk, t in alerts[:HIGHLIGHT_ROWS])
        b += f'<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>'
    else:
        b += f'<tr><td style="padding:8px 0 0;font-size:15px;color:{INK}">No Tier 1, Tier 2 or Watch List changes.</td></tr>'
    if rep.refilings_total:
        top = ", ".join(f"{_e(r.bank.name)} ({_e(r.bank.assets)})" for r in rep.refilings[:5])
        b += (f'<tr><td style="padding:10px 0 0;font-size:15px;color:{INK}"><b>{rep.refilings_total} refilings</b> with no material '
              f'capital change; largest: {top}.</td></tr>')
    if rep.new_filings_total:
        top = ", ".join(f"{_e(r.bank.name)} ({_e(r.bank.assets)})" for r in rep.new_filings[:5])
        b += (f'<tr><td style="padding:8px 0 0;font-size:15px;color:{INK}"><b>{rep.new_filings_total} other new filings</b> '
              f'with no red flags; largest: {top}.</td></tr>')

    b += (f'<tr><td style="padding:22px 0 0;font-size:13px;color:{MUTED}"><b style="color:{INK}">Attached:</b> the five-page PDF '
          f'and a spreadsheet with every filing, its capital ratios, flags, capital status and enforcement match, plus '
          f'summary analytics. <b style="color:{INK}">Sources:</b> {_a("FFIEC CDR", CDR_HOME)} · {_a("FDIC BankFind", BANKFIND_HOME)} · '
          f'Fed, OCC, FinCEN and FDIC enforcement records.</td></tr>')
    return _wrap(rep, b)
