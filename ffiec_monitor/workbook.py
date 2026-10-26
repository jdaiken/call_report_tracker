"""The spreadsheet attached to the daily/weekly email (replaces the plain CSV).

Sheets
  Summary              counts, capital-ratio distribution (count, min, quartiles, median, max,
                       number below each threshold) and an asset-size breakdown -- all live
                       formulas over the Filings sheet, with the thresholds as editable inputs
  Filings              every filing in the email: capital and other key ratios, capital status,
                       screen result and flags, enforcement match, links
  Enforcement          banks matched to an active order (with the discussion)
  Capital watch        less than well capitalized + community bank leverage ratio banks under 9%
  Flagged performance  flagged banks, this quarter vs. last
  Notes                definitions, thresholds, sources

Ratios are stored as fractions (0.0950) with a percent format. Formulas recalculate when the
file is opened (no cached values are written).
"""
from __future__ import annotations

import io
from typing import Optional

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .report import NAME, Report, _bank, capital_status, cblr_status
from .screen import screen, severity

FONT = "Arial"
INK, MUTED = "14181F", "5B626C"
HEAD_FILL = PatternFill("solid", fgColor="14181F")
SOFT_FILL = PatternFill("solid", fgColor="F2F4F6")
RED_FILL = PatternFill("solid", fgColor="F9D7D3")
AMBER_FILL = PatternFill("solid", fgColor="FCEBC5")
GREEN_FILL = PatternFill("solid", fgColor="DCEFE2")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="DDE0E4")
PCT = "0.00%;-0.00%;-"
PCT_CHG = "+0.00%;-0.00%;-"

# (column header, ratio key) -- the order of ratio columns on the Filings sheet
RATIO_COLS = [
    ("Leverage ratio", "tier1_leverage_ratio"),
    ("Tier 1 risk-based", "tier1_rbc_ratio"),
    ("Total risk-based", "total_rbc_ratio"),
    ("CET1", "cet1_ratio"),
    ("Return on assets", "roaa"),
    ("Net interest margin", "net_interest_margin"),
    ("Noncurrent loans + OREO", "noncurrent_loans_oreo_ratio"),
    ("Loans to deposits", "net_loans_leases_to_deposits"),
    ("Core deposits share", "core_deposits_to_total_deposits"),
    ("Brokered/reciprocal share", "brokered_reciprocal_deposits_to_total_deposits"),
    ("Non-core funding dependence", "net_noncore_funding_dependence_ratio"),
]
# Summary distribution rows: (ratio header, threshold, "below" or "above", what the threshold is)
THRESHOLDS = {
    "Leverage ratio": (0.05, "below", "PCA well-capitalized minimum"),
    "Tier 1 risk-based": (0.08, "below", "PCA well-capitalized minimum"),
    "Total risk-based": (0.10, "below", "PCA well-capitalized minimum"),
    "CET1": (0.065, "below", "PCA well-capitalized minimum"),
    "Return on assets": (0.0, "below", "losing money"),
    "Net interest margin": (0.025, "below", "squeezed margin (screen)"),
    "Noncurrent loans + OREO": (0.02, "above", "elevated problem assets"),
    "Loans to deposits": (1.10, "above", "relies on borrowed money (screen)"),
    "Core deposits share": (0.60, "below", "low core funding (screen)"),
    "Brokered/reciprocal share": (0.25, "above", "heavy network reliance (screen)"),
    "Non-core funding dependence": (0.35, "above", "heavy non-core reliance (screen)"),
}
SIZE_BUCKETS = [("Under $100M", 0, 100_000), ("$100M to $500M", 100_000, 500_000), ("$500M to $1B", 500_000, 1_000_000),
                ("$1B to $10B", 1_000_000, 10_000_000), ("$10B and over", 10_000_000, 10 ** 12)]


def _font(bold=False, color=INK, size=10, italic=False, underline=None):
    return Font(name=FONT, bold=bold, color=color, size=size, italic=italic, underline=underline)


def _header_row(ws, row: int, headers: list[str]) -> None:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = 30


def _title(ws, title: str, sub: str) -> None:
    ws["A1"] = title
    ws["A1"].font = _font(bold=True, size=14)
    ws["A2"] = sub
    ws["A2"].font = _font(color=MUTED)


def _frac(v) -> Optional[float]:
    return float(v) / 100 if isinstance(v, (int, float)) else None


def _widths(ws, widths: dict[str, float]) -> None:
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def _link(cell, url: Optional[str], text: str) -> None:
    if url:
        cell.value = text
        cell.hyperlink = url
        cell.font = _font(color="1D5FBF", underline="single")


def build_workbook(rep: Report, restatement_events, new_filing_events) -> bytes:
    wb = Workbook()
    label = rep.edition or f"{rep.run_date:%A, %B %-d, %Y}"
    enf = {}
    for m in rep.enforcement:
        if not m.action.stale:
            enf.setdefault(m.bank.rssd, m)

    # ---------------------------------------------------------- Filings
    fs = wb.active
    fs.title = "Filings"
    _title(fs, f"{NAME}: every filing", f"{label}. Ratios from each bank's Call Report. Filter with the header arrows.")
    headers = (["Type", "Institution", "City", "State", "RSSD", "FDIC cert", "Period", "Total assets ($000)"]
               + [h for h, _ in RATIO_COLS]
               + ["Capital status", "Screen", "Flags", "Enforcement", "Call Report", "UBPR", "FDIC profile"])
    hr = 4
    _header_row(fs, hr, headers)
    events = [("New filing", e) for e in new_filing_events] + [("Refiling", e) for e in restatement_events]
    events.sort(key=lambda t: t[1].meta.total_assets or 0, reverse=True)
    r = hr
    for kind, e in events:
        r += 1
        b = _bank(e.meta, e.period_end)
        ratios = getattr(e, "ratios", None) or {}
        cap = capital_status(ratios)
        cb = cblr_status(ratios)
        status = ("Below well capitalized" if cap else "CBLR under 9%" if cb else
                  "Well capitalized" if any(isinstance(ratios.get(k), (int, float)) for k in
                                            ("tier1_leverage_ratio", "total_rbc_ratio")) else "No data")
        flags = screen(ratios) if kind == "New filing" else []
        red, amber = severity(flags)
        m = enf.get(b.rssd)
        row = [kind, b.name, b.location.rsplit(",", 1)[0], b.location.rsplit(",", 1)[-1].strip(), b.rssd, b.cert or None,
               b.period, e.meta.total_assets]
        row += [_frac(ratios.get(k)) for _, k in RATIO_COLS]
        row += [status, "Red" if red else "Amber" if amber else ("None" if ratios else ""),
                "; ".join(f.text for f in flags),
                (f"{m.action.agency} {m.action.action}" + (f" ({m.action.start:%Y-%m-%d})" if m.action.start else "")) if m else ""]
        for c, v in enumerate(row, 1):
            cell = fs.cell(row=r, column=c, value=v)
            cell.font = _font()
            cell.border = Border(bottom=THIN)
        _link(fs.cell(row=r, column=len(row) + 1), b.call_report_url, "Call Report")
        _link(fs.cell(row=r, column=len(row) + 2), b.ubpr_url, "UBPR")
        _link(fs.cell(row=r, column=len(row) + 3), b.bankfind_url, "FDIC profile")
    last = max(r, hr + 1)
    first_ratio = 9
    for c in range(first_ratio, first_ratio + len(RATIO_COLS)):
        for row_ in range(hr + 1, last + 1):
            fs.cell(row=row_, column=c).number_format = PCT
    for row_ in range(hr + 1, last + 1):
        fs.cell(row=row_, column=8).number_format = "#,##0"
    ncol = len(headers)
    col = {h: get_column_letter(i) for i, h in enumerate(headers, 1)}
    rng = lambda h: f"{col[h]}{hr + 1}:{col[h]}{last}"                       # noqa: E731
    # Highlight ratios below the PCA minimums, and the status / screen / enforcement cells.
    for h, (thr, way, _why) in THRESHOLDS.items():
        if h in ("Leverage ratio", "Tier 1 risk-based", "Total risk-based", "CET1"):
            fs.conditional_formatting.add(rng(h), FormulaRule(
                formula=[f'AND(ISNUMBER({col[h]}{hr + 1}),{col[h]}{hr + 1}<{thr})'], fill=RED_FILL))
    fs.conditional_formatting.add(rng("Capital status"), CellIsRule(operator="equal", formula=['"Below well capitalized"'], fill=RED_FILL))
    fs.conditional_formatting.add(rng("Capital status"), CellIsRule(operator="equal", formula=['"CBLR under 9%"'], fill=AMBER_FILL))
    fs.conditional_formatting.add(rng("Screen"), CellIsRule(operator="equal", formula=['"Red"'], fill=RED_FILL))
    fs.conditional_formatting.add(rng("Screen"), CellIsRule(operator="equal", formula=['"Amber"'], fill=AMBER_FILL))
    fs.conditional_formatting.add(rng("Enforcement"), FormulaRule(formula=[f'LEN({col["Enforcement"]}{hr + 1})>0'], fill=RED_FILL))
    if r > hr:
        t = Table(displayName="Filings", ref=f"A{hr}:{get_column_letter(ncol)}{last}")
        t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
        fs.add_table(t)
    fs.freeze_panes = f"C{hr + 1}"
    _widths(fs, {"A": 11, "B": 34, "C": 16, "D": 6, "E": 10, "F": 9, "G": 9, "H": 14})
    for i in range(first_ratio, first_ratio + len(RATIO_COLS)):
        fs.column_dimensions[get_column_letter(i)].width = 12
    _widths(fs, {col["Capital status"]: 22, col["Screen"]: 9, col["Flags"]: 60, col["Enforcement"]: 38,
                 col["Call Report"]: 12, col["UBPR"]: 8, col["FDIC profile"]: 12})

    # ---------------------------------------------------------- Summary (formulas over Filings)
    ss = wb.create_sheet("Summary", 0)
    _title(ss, f"{NAME}: summary", f"{label}. Every figure below is a live formula over the Filings sheet.")
    F = "Filings!"

    def frng(h):
        return f"{F}${col[h]}${hr + 1}:${col[h]}${last}"

    ss["A4"] = "Activity"
    ss["A4"].font = _font(bold=True, size=12)
    counts = [
        ("Filings in this report", f"=COUNTA({frng('Institution')})"),
        ("New filings", f'=COUNTIF({frng("Type")},"New filing")'),
        ("Refilings", f'=COUNTIF({frng("Type")},"Refiling")'),
        ("Below well capitalized", f'=COUNTIF({frng("Capital status")},"Below well capitalized")'),
        ("CBLR banks under 9%", f'=COUNTIF({frng("Capital status")},"CBLR under 9%")'),
        ("New filings with a red flag", f'=COUNTIF({frng("Screen")},"Red")'),
        ("New filings with amber flags only", f'=COUNTIF({frng("Screen")},"Amber")'),
        ("Banks under an active enforcement order", f'=COUNTIF({frng("Enforcement")},"?*")'),
        ("Total assets of filers ($000)", f"=SUM({frng('Total assets ($000)')})"),
    ]
    for i, (k, f) in enumerate(counts, 5):
        ss.cell(row=i, column=1, value=k).font = _font()
        c = ss.cell(row=i, column=2, value=f)
        c.font = _font(bold=True)
        c.number_format = "#,##0"
        if k.startswith(("Below", "Banks under", "New filings with a red")):
            ss.cell(row=i, column=1).font = _font(bold=True)

    top = 5 + len(counts) + 2
    ss.cell(row=top, column=1, value="Capital and key ratios across filers").font = _font(bold=True, size=12)
    ss.cell(row=top + 1, column=1, value="Thresholds (yellow) are editable; the count columns update.").font = _font(color=MUTED, italic=True)
    dh = ["Ratio", "Filers reporting", "Minimum", "25th percentile", "Median", "75th percentile", "Maximum",
          "Threshold", "Test", "Filers past threshold", "% past threshold", "What the threshold is"]
    _header_row(ss, top + 2, dh)
    for i, (h, _k) in enumerate(RATIO_COLS):
        rr = top + 3 + i
        thr, way, why = THRESHOLDS[h]
        src = frng(h)
        vals = [h, f"=COUNT({src})", f"=IF(B{rr}=0,\"\",MIN({src}))", f"=IF(B{rr}=0,\"\",QUARTILE({src},1))",
                f"=IF(B{rr}=0,\"\",MEDIAN({src}))", f"=IF(B{rr}=0,\"\",QUARTILE({src},3))", f"=IF(B{rr}=0,\"\",MAX({src}))",
                thr, way,
                f'=IF(I{rr}="below",COUNTIF({src},"<"&H{rr}),COUNTIF({src},">"&H{rr}))',
                f"=IF(B{rr}=0,\"\",J{rr}/B{rr})", why]
        for c, v in enumerate(vals, 1):
            cell = ss.cell(row=rr, column=c, value=v)
            cell.font = _font(bold=(c == 1))
            cell.border = Border(bottom=THIN)
            if 3 <= c <= 8:
                cell.number_format = PCT
        ss.cell(row=rr, column=8).fill = INPUT_FILL
        ss.cell(row=rr, column=8).font = _font(color="0000FF")
        ss.cell(row=rr, column=11).number_format = "0.0%"
    ss.cell(row=top + 3, column=8).comment = Comment(
        "PCA well-capitalized minimums: 12 CFR 324.403 (leverage 5%, Tier 1 risk-based 8%, total risk-based 10%, CET1 6.5%). "
        "Other thresholds are the monitor's screen levels (rules of thumb).", "Monitor")
    ss.conditional_formatting.add(f"J{top + 3}:J{top + 6}", CellIsRule(operator="greaterThan", formula=["0"], fill=RED_FILL))

    bt = top + 3 + len(RATIO_COLS) + 2
    ss.cell(row=bt, column=1, value="By asset size").font = _font(bold=True, size=12)
    bh = ["Asset size", "From ($000)", "To ($000)", "Filers", "% of filers", "Median leverage ratio", "Red-flagged",
          "Below well capitalized", "CBLR under 9%"]
    _header_row(ss, bt + 1, bh)
    A, L = frng("Total assets ($000)"), frng("Leverage ratio")
    for i, (name, lo, hi) in enumerate(SIZE_BUCKETS):
        rr = bt + 2 + i
        vals = [name, lo, hi,
                f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr})',
                f"=IF($B$5=0,\"\",D{rr}/$B$5)",
                # MEDIAN(IF(...)) as an array formula is fragile across Excel/LibreOffice; use AVERAGEIFS as the centre.
                f'=IFERROR(AVERAGEIFS({L},{A},">="&B{rr},{A},"<"&C{rr}),"")',
                f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr},{frng("Screen")},"Red")',
                f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr},{frng("Capital status")},"Below well capitalized")',
                f'=COUNTIFS({A},">="&B{rr},{A},"<"&C{rr},{frng("Capital status")},"CBLR under 9%")']
        for c, v in enumerate(vals, 1):
            cell = ss.cell(row=rr, column=c, value=v)
            cell.font = _font(bold=(c == 1))
            cell.border = Border(bottom=THIN)
        ss.cell(row=rr, column=2).number_format = ss.cell(row=rr, column=3).number_format = "#,##0"
        ss.cell(row=rr, column=5).number_format = "0.0%"
        ss.cell(row=rr, column=6).number_format = PCT
    ss.cell(row=bt + 1, column=6, value="Average leverage ratio")
    ss.cell(row=bt + 1, column=6).font = _font(bold=True, color="FFFFFF")
    _widths(ss, {"A": 36, "B": 14, "C": 13, "D": 14, "E": 12, "F": 14, "G": 12, "H": 12, "I": 9, "J": 13, "K": 12, "L": 38})
    ss.freeze_panes = "A4"

    # ---------------------------------------------------------- Enforcement
    es = wb.create_sheet("Enforcement")
    _title(es, "Banks under enforcement orders", "Active Fed, OCC and FDIC orders and agreements; FinCEN BSA actions, last 3 years. "
           "Civil money penalties excluded.")
    eh = ["Institution", "Location", "Total assets", "Agency", "Order", "Issued", "Type", "Matched on", "Discussion", "Order link",
          "Call Report", "FDIC profile", "UBPR"]
    _header_row(es, 4, eh)
    rr = 4
    for m in [x for x in rep.enforcement if not x.action.stale]:
        rr += 1
        note = rep.enforcement_notes.get(m.bank.rssd, ("", ""))[0]
        vals = [m.bank.name, m.bank.location, m.bank.assets, m.action.agency, m.action.action, m.action.start, m.action.topic,
                m.how, note]
        for c, v in enumerate(vals, 1):
            cell = es.cell(row=rr, column=c, value=v)
            cell.font = _font()
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        es.cell(row=rr, column=6).number_format = "yyyy-mm-dd"
        _link(es.cell(row=rr, column=10), m.action.url, "Order")
        _link(es.cell(row=rr, column=11), m.bank.call_report_url, "Call Report")
        _link(es.cell(row=rr, column=12), m.bank.bankfind_url, "FDIC profile")
        _link(es.cell(row=rr, column=13), m.bank.ubpr_url, "UBPR")
    stale = [m for m in rep.enforcement if m.action.stale]
    if stale:
        es.cell(row=rr + 2, column=1, value="Not counted (over 10 years old, no termination date; most likely closed): "
                + "; ".join(f"{m.bank.name} ({m.action.agency}, {m.action.start:%Y})" for m in stale)).font = _font(color=MUTED, italic=True)
    if rr == 4:
        es.cell(row=5, column=1, value="No bank in this report is under an active order.").font = _font(italic=True)
    _widths(es, {"A": 30, "B": 20, "C": 11, "D": 8, "E": 30, "F": 11, "G": 22, "H": 12, "I": 80, "J": 9, "K": 12, "L": 12})

    # ---------------------------------------------------------- Capital watch
    cs = wb.create_sheet("Capital watch")
    _title(cs, "Capital watch", "Below a PCA well-capitalized minimum, and community bank leverage ratio (CBLR) banks under 9%.")
    _header_row(cs, 4, ["Group", "Institution", "Location", "Total assets", "Period", "Detail", "Call Report", "FDIC profile", "UBPR"])
    rr = 4
    for grp, items in (("Below well capitalized", rep.ltwc), ("CBLR under 9%", rep.cblr)):
        for b, rs in items:
            rr += 1
            for c, v in enumerate([grp, b.name, b.location, b.assets, b.period, "; ".join(rs)], 1):
                cell = cs.cell(row=rr, column=c, value=v)
                cell.font = _font(bold=(c == 1))
                cell.fill = RED_FILL if grp.startswith("Below") else AMBER_FILL if c == 1 else PatternFill()
            _link(cs.cell(row=rr, column=7), b.call_report_url, "Call Report")
            _link(cs.cell(row=rr, column=8), b.bankfind_url, "FDIC profile")
            _link(cs.cell(row=rr, column=9), b.ubpr_url, "UBPR")
    if rr == 4:
        cs.cell(row=5, column=1, value="None.").font = _font(italic=True)
    _widths(cs, {"A": 22, "B": 34, "C": 22, "D": 11, "E": 9, "F": 90, "G": 12, "H": 12})

    # ---------------------------------------------------------- Flagged performance
    ps = wb.create_sheet("Flagged performance")
    prior = rep.performance[0].prior_label if rep.performance else "last quarter"
    _title(ps, "How the flagged banks are performing", f"This quarter vs. {prior}. Change = this quarter minus last quarter.")
    from .performance import METRICS
    ph = ["Institution", "Location", "Why flagged", "Read"]
    for _k, lab, _g in METRICS:
        ph += [f"{lab}: last", f"{lab}: now", f"{lab}: change"]
    _header_row(ps, 4, ph)
    rr = 4
    for bp in rep.performance:
        rr += 1
        vals = [bp.bank.name, bp.bank.location, "; ".join(bp.why), bp.read]
        for row_ in bp.rows:
            vals += [None if row_.prior is None else row_.prior / 100, None if row_.current is None else row_.current / 100]
            vals.append(f"=IF(AND(ISNUMBER({get_column_letter(len(vals) - 1)}{rr}),ISNUMBER({get_column_letter(len(vals))}{rr})),"
                        f"{get_column_letter(len(vals))}{rr}-{get_column_letter(len(vals) - 1)}{rr},\"\")")
        for c, v in enumerate(vals, 1):
            cell = ps.cell(row=rr, column=c, value=v)
            cell.font = _font(bold=(c == 1))
            if c > 4:
                cell.number_format = PCT_CHG if (c - 4) % 3 == 0 else PCT
        for j, row_ in enumerate(bp.rows):
            ch = ps.cell(row=rr, column=4 + 3 * j + 3)
            if row_.better is True:
                ch.fill = GREEN_FILL
            elif row_.better is False:
                ch.fill = RED_FILL
    if rr == 4:
        ps.cell(row=5, column=1, value="No flagged banks.").font = _font(italic=True)
    ps.freeze_panes = "B5"
    _widths(ps, {"A": 30, "B": 20, "C": 30, "D": 60})
    for i in range(5, len(ph) + 1):
        ps.column_dimensions[get_column_letter(i)].width = 12

    # ---------------------------------------------------------- Notes
    ns = wb.create_sheet("Notes")
    notes = [
        ("Capital status", "Below well capitalized = under a PCA well-capitalized minimum (leverage 5%, Tier 1 risk-based 8%, "
                           "total risk-based 10%, CET1 6.5%; 12 CFR 324.403). CBLR under 9% = a community bank leverage ratio "
                           "bank (reports only a leverage ratio) under 9%: still well capitalized in the two-quarter grace period "
                           "if 8-9%."),
        ("Screen", "Red = at least one concern (capital near the line, losses, loans over 110% of deposits, non-core funding "
                   "over 35%, core deposits under 60%, brokered/reciprocal over 25%, or thin capital with heavy brokered/"
                   "reciprocal funding). Amber = milder versions. New filings only."),
        ("Enforcement", "Active Fed and OCC orders and agreements (name + state match), FDIC orders from the ED&O export "
                        "(FDIC cert match), FinCEN BSA actions in the last 3 years. Civil money penalties excluded."),
        ("Ratios", "Computed from each bank's Call Report (FFIEC CDR). ROA and NIM are annualized year-to-date."),
        ("Sources", "FFIEC Central Data Repository (https://cdr.ffiec.gov/public/); FDIC BankFind; federalreserve.gov "
                    "enforcement actions; OCC EASearch; FinCEN enforcement actions; FDIC Enforcement Decisions and Orders."),
    ]
    _title(ns, "Notes", "Definitions and sources")
    for i, (k, v) in enumerate(notes, 4):
        ns.cell(row=i, column=1, value=k).font = _font(bold=True)
        c = ns.cell(row=i, column=2, value=v)
        c.font = _font()
        c.alignment = Alignment(wrap_text=True, vertical="top")
    _widths(ns, {"A": 18, "B": 120})

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
