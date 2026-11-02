"""Build the "call report pipeline field spec: Q3 2026 rerun" file and email it.

One xlsx, one tab named `pipeline_data`: one row per bank per quarter for 2025Q2-2026Q3, every
header from the spec's Template tab in that order (HEADERS below, copied verbatim).

Data sources (all reached from the Actions runner, not from a dev box):
  * FFIEC CDR bulk "Call Reports -- Single Period" zip, tab-delimited, one per quarter (all banks,
    all schedules, plus the Panel of Reporters for name/state/cert/form). No API quota used.
  * Fallback per quarter if the bulk page cannot be driven: the PWS API (Panel of Reporters +
    one RetrieveFacsimile per bank), throttled and capped by --api-budget-minutes.
  * FDIC BankFind institutions: holding company RSSD (RSSDHCR) -> PARENT NUMBER / HOLD TYPE.
  * FRED DGS10 / DGS30: last business day of each quarter.

Format rules from the spec's Read Me: dollars in $000 as filed; income year-to-date as filed; one
header per item (RCFD for 031 filers, RCON otherwise -- the two are never both populated for the
same bank); not reported = blank, never 0; RC-R ratios as percent numbers; ids as integers.
The 45 existing fields are filled for 2026Q2 and 2026Q3 only, with the same code as the current
Q3 run (ffiec_monitor.pipeline_fields.extract), IDC-only columns blank as before.
Holding companies (FR Y-9C, HOLD TYPE 1) are not included: the spec allows banks now, HCs later.

  python scripts/build_pipeline_spec.py [--out FILE] [--no-email] [--api-budget-minutes N]
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rates_newsletter"))

import requests

from ffiec_monitor import pipeline_fields
from ffiec_monitor.ratios import _num

log = logging.getLogger("pipeline_spec")
csv.field_size_limit(10_000_000)

QUARTERS = [date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31),
            date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]
EXISTING_QUARTERS = {date(2026, 6, 30), date(2026, 9, 30)}
UA = "call-report-tracker/1.0 (+https://github.com/jdaiken/call_report_tracker)"
BULK_URL = "https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx"

# Template tab, row 1, verbatim.
IDENT = ["FRS ID", "CERTIFICATE NUMBER", "INSTITUTION NAME", "STATE", "PARENT NUMBER", "HOLD TYPE",
         "YEAR PERIOD", "FORM", "CBLR"]
EXISTING = [h for h, _f in pipeline_fields.FIELDS if h not in
            {"FRS ID", "CERTIFICATE NUMBER", "INSTITUTION NAME", "STATE", "PARENT NUMBER", "HOLD TYPE", "YEAR PERIOD"}]
NEW = [
    "RC_2170", "RC_0081", "RC_0071", "RC_B987", "RC_B989", "RC_1754", "RC_1771", "RC_1772", "RC_1773", "RC_JA22",
    "RC_5369", "RC_B528", "RC_3123", "RC_B529", "RC_2150", "RC_3163", "RC_0426", "RC_2200", "RC_6631", "RC_6636",
    "RC_B993", "RC_B995", "RC_3548", "RC_3190", "RC_3200", "RC_2948", "RC_3838", "RC_B530", "RC_3210", "RC_G105",
    "RC_0416", "RC_2365", "RC_JH83", "RC_J473", "RC_J474", "RC_F049", "RC_F051", "RC_F052", "RC_5597",
    "Uninsured deposits (estimated, all banks)", "RC_F055", "RC_F056", "RC_F057", "RC_F058", "FHLB borrowing capacity",
    "RC_F158", "RC_F159", "RC_1420", "RC_1797", "RC_5367", "RC_5368", "RC_1460", "RC_F160", "RC_F161", "RC_1590",
    "RC_1766", "RC_2746", "RC_2122", "RC_1406", "RC_1407", "RC_1403", "RI_4635", "RI_4605", "RI_4010", "RI_B488",
    "RI_B489", "RI_4060", "RI_4107", "RI_4508", "RI_0093", "RI_HK03", "RI_HK04", "RI_4180", "RI_4185", "RI_4200",
    "RI_4073", "RI_4074", "RI_JJ33", "RI_4079", "RI_3521", "RI_3196", "RI_4093", "RI_4301", "RI_4302", "RI_4340",
    "RI_4460", "RI_4470", "RCK_3381", "RCK_B558", "RCK_B559", "RCK_B560", "RCK_3365", "RCK_3360", "RCK_3368",
    "RCK_3485", "RCK_B563", "RCK_HK16", "RCK_HK17", "RCK_3404", "RCK_3353", "RCK_3355", "RCR_8274", "RCR_P859",
    "RCR_3792", "RCR_A223", "RCR_7204", "RCR_P793", "RCR_7206", "RCR_7205", "UST10Y", "UST30Y",
]
HEADERS = IDENT + EXISTING + NEW
VERIFY = ["RC_J473", "RC_F049", "RC_F055", "RC_F056", "RC_F057", "RC_F058", "RI_HK03", "RI_HK04",
          "RCK_HK16", "RCK_HK17", "RCR_P859"]

# Prefix try-order per header group. RCFD/RCON (and RCFA/RCOA) never both populated for one bank.
PREFIXES = {"RC": ("RCFD", "RCON", "RCFN"), "RI": ("RIAD",), "RCK": ("RCFD", "RCON", "RCFN"),
            "RCR": ("RCFA", "RCOA", "RCFD", "RCON")}
# Items that are the sum of domestic and foreign offices on the 031 (spec: 2200 = RCON + RCFN).
SUMMED = {"RC_2200": ("RCON2200", "RCFN2200"), "RCK_3360": ("RCON3360", "RCFN3360")}
CBLR_CODES = ("RCOALE74", "RCFALE74")
RATIOS = {"RCR_7204", "RCR_P793", "RCR_7206", "RCR_7205"}     # percent numbers, kept as floats


def wanted_codes() -> set[str]:
    """Only the MDRM codes we use, so six quarters of all-schedule bulk data fit in memory."""
    codes = set(CBLR_CODES) | {c for pair in SUMMED.values() for c in pair}
    for h in NEW:
        grp, _, item = h.partition("_")
        if grp in PREFIXES and len(item) == 4:
            codes |= {p + item for p in PREFIXES[grp]}
    src = (ROOT / "ffiec_monitor" / "pipeline_fields.py").read_text()
    src += (ROOT / "ffiec_monitor" / "ratios.py").read_text()
    codes |= set(re.findall(r'"((?:RC[A-Z]{2}|RIAD)[0-9A-Z]{4})"', src))
    return codes


# ---------------------------------------------------------------- FFIEC bulk download

def _form_fields(doc) -> dict[str, str]:
    """Serialize an ASP.NET WebForms page's form the way a browser would (no submit buttons)."""
    data: dict[str, str] = {}
    for el in doc.xpath("//form//input"):
        name, typ = el.get("name"), (el.get("type") or "text").lower()
        if not name or typ in ("submit", "button", "image", "reset", "file"):
            continue
        if typ in ("radio", "checkbox") and el.get("checked") is None:
            continue
        data[name] = el.get("value") or ""
    for sel in doc.xpath("//form//select"):
        name = sel.get("name")
        if not name:
            continue
        opts = sel.xpath(".//option")
        chosen = [o for o in opts if o.get("selected") is not None] or opts[:1]
        if chosen:
            data[name] = chosen[0].get("value") or chosen[0].text_content().strip()
    return data


def _select(doc, name_part: str):
    hits = [s for s in doc.xpath("//select") if name_part.lower() in (s.get("name") or "").lower()]
    return hits[0] if hits else None


def download_bulk(period: date, session: Optional[requests.Session] = None) -> bytes:
    """Drive cdr.ffiec.gov's bulk-download page: product = Call Reports -- Single Period,
    the quarter's date, tab-delimited, Download. Returns the zip bytes or raises."""
    from lxml import html as lhtml

    s = session or requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(BULK_URL, timeout=120)
    r.raise_for_status()
    doc = lhtml.fromstring(r.content)

    prod = _select(doc, "ListBox1")
    if prod is None:
        raise RuntimeError("bulk page: product list not found")
    opt = [o for o in prod.xpath(".//option") if "single period" in o.text_content().lower()
           and "call" in o.text_content().lower()]
    if not opt:
        raise RuntimeError("bulk page: 'Call Reports -- Single Period' option not found")
    data = _form_fields(doc)
    data[prod.get("name")] = opt[0].get("value")
    data["__EVENTTARGET"] = prod.get("name")          # the list auto-posts back to load its dates
    data["__EVENTARGUMENT"] = ""
    r = s.post(BULK_URL, data=data, timeout=120)
    r.raise_for_status()
    doc = lhtml.fromstring(r.content)

    dates = _select(doc, "DatesDropDownList")
    if dates is None:
        raise RuntimeError("bulk page: date list not found")
    want = period.strftime("%m/%d/%Y")
    dopt = [o for o in dates.xpath(".//option") if o.text_content().strip() == want]
    if not dopt:
        avail = [o.text_content().strip() for o in dates.xpath(".//option")][:8]
        raise RuntimeError(f"bulk page: {want} not offered yet (latest: {avail})")
    data = _form_fields(doc)
    data[dates.get("name")] = dopt[0].get("value")
    data[_select(doc, "ListBox1").get("name")] = opt[0].get("value")
    for el in doc.xpath("//input[@type='radio']"):           # tab-delimited format
        if "tsv" in (el.get("value") or "").lower() or "tsv" in (el.get("id") or "").lower():
            data[el.get("name")] = el.get("value")
    btn = [el for el in doc.xpath("//input[@type='submit']")
           if (el.get("value") or "").strip().lower() == "download"]
    if not btn:
        raise RuntimeError("bulk page: Download button not found")
    data[btn[0].get("name")] = btn[0].get("value")
    data["__EVENTTARGET"] = ""
    data["__EVENTARGUMENT"] = ""
    r = s.post(BULK_URL, data=data, timeout=600)
    r.raise_for_status()
    if not r.content.startswith(b"PK"):
        raise RuntimeError(f"bulk download did not return a zip ({r.headers.get('Content-Type')}, "
                           f"{len(r.content):,} bytes)")
    return r.content


def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace", newline="")
        yield from csv.reader(text, delimiter="\t")


def parse_bulk(zbytes: bytes, codes: set[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    """-> (panel {rssd: {name, state, cert, form}}, concepts {rssd: {MDRM: raw str}})."""
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    panel: dict[str, dict] = {}
    concepts: dict[str, dict] = {}
    for name in z.namelist():
        low = name.lower()
        if not low.endswith(".txt"):
            continue
        if " por " in f" {low} " or "panel of reporters" in low or low.split("/")[-1].startswith("ffiec cdr call bulk por"):
            it = _rows(z, name)
            hdr = [h.strip().strip('"') for h in next(it)]
            col = {h.lower(): i for i, h in enumerate(hdr)}

            def g(row, *keys):
                for k in keys:
                    for h, i in col.items():
                        if k in h and i < len(row) and row[i].strip():
                            return row[i].strip().strip('"')
                return ""
            for row in it:
                rssd = g(row, "idrssd")
                if not rssd.isdigit():
                    continue
                panel[rssd] = {"name": g(row, "financial institution name"),
                               "state": g(row, "financial institution state"),
                               "cert": g(row, "fdic certificate"),
                               "form": g(row, "filing type")}
            continue
        if "narr" in low:              # free-text narrative schedule
            continue
        it = _rows(z, name)
        try:
            hdr = [h.strip().strip('"') for h in next(it)]
        except StopIteration:
            continue
        if not hdr or hdr[0].upper() != "IDRSSD":
            continue
        keep = [(i, h) for i, h in enumerate(hdr) if h in codes]
        next(it, None)                 # second row: item descriptions
        if not keep:
            continue
        for row in it:
            if not row or not row[0].strip().strip('"').isdigit():
                continue
            d = concepts.setdefault(row[0].strip().strip('"'), {})
            for i, h in keep:
                if i < len(row):
                    v = row[i].strip().strip('"')
                    if v != "":
                        d[h] = v
    return panel, concepts


# ---------------------------------------------------------------- API fallback

def load_via_api(period: date, codes: set[str], deadline: float) -> tuple[dict, dict, int]:
    from ffiec_monitor.config import FFIECConfig
    from ffiec_monitor.ffiec_client import FFIECAuthError, FFIECClient, FFIECRateLimitError

    client = FFIECClient(FFIECConfig.from_env())
    panel = {}
    for p in client.retrieve_panel_of_reporters(period):
        rssd = str(p.get("ID_RSSD") or "").strip()
        if rssd.isdigit():
            form = next((str(v) for k, v in p.items() if "filing" in k.lower() or "form" in k.lower()), "")
            panel[rssd] = {"name": str(p.get("Name") or "").strip(), "state": str(p.get("State") or "").strip(),
                           "cert": str(p.get("FDICCertNumber") or "").strip(), "form": form}
    concepts, left = {}, 0
    for i, rssd in enumerate(sorted(panel)):
        if time.monotonic() > deadline:
            left = len(panel) - i
            log.warning("%s: API time budget used up, %d banks not pulled", period, left)
            break
        try:
            fac = client.retrieve_facsimile(rssd, period)
        except (FFIECAuthError, FFIECRateLimitError):
            left = len(panel) - i
            log.exception("%s: stopping API pulls", period)
            break
        except Exception as e:                       # noqa: BLE001
            log.warning("%s %s: %s", period, rssd, e)
            continue
        c = fac.get("concepts", fac)
        concepts[rssd] = {k: v for k, v in c.items() if k in codes and str(v).strip() != ""}
        if (i + 1) % 250 == 0:
            log.info("%s: pulled %d / %d via API", period, i + 1, len(panel))
    return panel, concepts, left


# ---------------------------------------------------------------- other sources

def fdic_parents() -> tuple[dict[str, int], dict[str, int]]:
    """({rssd: RSSDHCR}, {cert: RSSDHCR}) for every FDIC institution, active or not (current
    holding company as BankFind has it today). ({}, {}) if BankFind is unreachable."""
    by_rssd, by_cert = {}, {}
    try:
        offset = 0
        while True:
            r = requests.get("https://api.fdic.gov/banks/institutions", timeout=120, headers={"User-Agent": UA},
                             params={"fields": "CERT,FED_RSSD,RSSDHCR", "limit": 10000, "offset": offset,
                                     "format": "json", "sort_by": "CERT", "sort_order": "ASC"})
            r.raise_for_status()
            data = r.json().get("data", [])
            for d in data:
                x = d.get("data", d)
                hc = int(_num(x.get("RSSDHCR")) or 0)
                if x.get("FED_RSSD") not in (None, "", 0, "0"):
                    by_rssd[str(int(_num(x["FED_RSSD"])))] = hc
                if x.get("CERT") not in (None, ""):
                    by_cert[str(int(_num(x["CERT"])))] = hc
            if len(data) < 10000:
                break
            offset += 10000
    except Exception as e:                           # noqa: BLE001
        log.warning("FDIC BankFind lookup failed: %s", e)
    return by_rssd, by_cert


def treasury_yields() -> dict[date, dict[str, Optional[float]]]:
    from rates_digest.fetch import fetch_series

    out: dict[date, dict[str, Optional[float]]] = {q: {} for q in QUARTERS}
    for sid, col in (("DGS10", "UST10Y"), ("DGS30", "UST30Y")):
        obs = fetch_series(sid, date(2025, 6, 1))
        if obs.error:
            log.warning("FRED %s: %s", sid, obs.error)
        for q in QUARTERS:
            pts = [v for d, v in obs.points if d <= q]
            out[q][col] = pts[-1] if pts else None
    return out


# ---------------------------------------------------------------- row building

def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


def _val(c: dict, header: str) -> Optional[float]:
    if header in SUMMED:
        vals = [_num(c.get(k)) for k in SUMMED[header]]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    grp, _, item = header.partition("_")
    for p in PREFIXES[grp]:
        v = _num(c.get(p + item))
        if v is not None:
            return v
    return None


def _cblr(c: dict) -> Optional[str]:
    for code in CBLR_CODES:
        raw = str(c.get(code, "")).strip().lower()
        if raw in ("1", "1.0", "true", "y", "yes"):
            return "Y"
        if raw in ("0", "0.0", "false", "n", "no"):
            return "N"
    if not c:
        return None
    lev = _num(c.get("RCOA7204")) if c.get("RCOA7204") is not None else _num(c.get("RCFA7204"))
    risk_based = any(_num(c.get(k)) is not None for k in ("RCOA7206", "RCFA7206", "RCOA7205", "RCFA7205"))
    return ("N" if risk_based else "Y") if lev is not None else None


def _form(raw: str, c: dict) -> Optional[str]:
    m = re.search(r"0(31|41|51)", raw or "")
    if m:
        return "0" + m.group(1)
    if any(k.startswith("RCFD") for k in c):
        return "031"
    return None


def build_rows(quarters: dict[date, tuple[dict, dict]], parents: tuple[dict, dict],
               ust: dict[date, dict]) -> list[dict]:
    by_rssd, by_cert = parents
    rows = []
    for q in QUARTERS:
        if q not in quarters:
            continue
        panel, concepts = quarters[q]
        prior = quarters.get(QUARTERS[QUARTERS.index(q) - 1]) if QUARTERS.index(q) else None
        qtag = f"{q.year}Q{(q.month - 1) // 3 + 1}"
        for rssd in sorted(concepts):               # banks with data this quarter (non-filers dropped)
            meta, c = panel.get(rssd, {}), concepts.get(rssd, {})
            if not meta.get("form"):                 # API panel may lack the form: borrow another quarter's
                meta = {**meta, "form": next((p[0][rssd]["form"] for p in quarters.values()
                                              if rssd in p[0] and p[0][rssd].get("form")), "")}
            cert = _int(meta.get("cert"))
            cert = cert if cert else None
            parent = by_rssd.get(rssd)
            if parent is None and cert is not None:
                parent = by_cert.get(str(cert))
            row: dict[str, Any] = {
                "FRS ID": int(rssd), "CERTIFICATE NUMBER": cert, "INSTITUTION NAME": meta.get("name") or None,
                "STATE": meta.get("state") or None, "PARENT NUMBER": parent,
                "HOLD TYPE": None if parent is None else (2 if parent else 0),
                "YEAR PERIOD": qtag, "FORM": _form(meta.get("form", ""), c), "CBLR": _cblr(c),
            }
            if q in EXISTING_QUARTERS and c:
                pf = pipeline_fields.extract(c, q)
                pprev = pipeline_fields.extract(prior[1][rssd], QUARTERS[QUARTERS.index(q) - 1]) \
                    if prior and rssd in prior[1] else None
                pf = pipeline_fields.with_prior(pf, pprev)
                for h in EXISTING:
                    v = pf.get(h)
                    row[h] = round(v, 4) if isinstance(v, float) else v
            for h in NEW:
                if h in ("UST10Y", "UST30Y"):
                    row[h] = ust.get(q, {}).get(h)
                elif h == "Uninsured deposits (estimated, all banks)":
                    v5597 = _val(c, "RC_5597")
                    f051, f052 = _val(c, "RC_F051"), _val(c, "RC_F052")
                    row[h] = v5597 if v5597 is not None else (
                        f051 - 250 * f052 if f051 is not None and f052 is not None else None)
                elif h == "FHLB borrowing capacity":
                    row[h] = None
                else:
                    v = _val(c, h)
                    row[h] = int(v) if v is not None and h not in RATIOS and float(v).is_integer() else v
            rows.append(row)
    rows.sort(key=lambda r: (r["YEAR PERIOD"], -(r.get("RC_2170") or 0)))
    return rows


def write_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("pipeline_data")
    ws.freeze_panes = "D2"
    dark, light = PatternFill("solid", fgColor="1F3A5F"), PatternFill("solid", fgColor="4A6C8F")
    head = []
    for h in HEADERS:
        cell = WriteOnlyCell(ws, value=h)
        cell.fill = light if h in EXISTING else dark
        cell.font = Font(bold=True, color="FFFFFF")
        head.append(cell)
    ws.append(head)
    for r in rows:
        ws.append([r.get(h) for h in HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def coverage(rows: list[dict], qtag: str) -> list[tuple[str, int, int]]:
    sub = [r for r in rows if r["YEAR PERIOD"] == qtag]
    return [(h, sum(1 for r in sub if r.get(h) is not None), len(sub)) for h in HEADERS]


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--api-budget-minutes", type=float, default=240.0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    codes = wanted_codes()
    deadline = time.monotonic() + a.api_budget_minutes * 60
    quarters: dict[date, tuple[dict, dict]] = {}
    status: dict[date, str] = {}
    for q in QUARTERS:
        try:
            zb = download_bulk(q)
            panel, concepts = parse_bulk(zb, codes)
            if len(concepts) < 1000:
                raise RuntimeError(f"bulk zip parsed to only {len(concepts)} banks")
            quarters[q] = (panel, concepts)
            status[q] = f"FFIEC bulk file ({len(concepts):,} banks)"
            log.info("%s: bulk OK, %d panel, %d banks with data", q, len(panel), len(concepts))
        except Exception as e:                       # noqa: BLE001
            log.warning("%s: bulk download failed (%s); trying the PWS API", q, e)
            try:
                panel, concepts, left = load_via_api(q, codes, deadline)
                quarters[q] = (panel, concepts)
                status[q] = (f"FFIEC PWS API ({len(concepts):,} banks"
                             + (f"; {left:,} not reached in the time budget" if left else "") + ")")
            except Exception as e2:                  # noqa: BLE001
                log.exception("%s: API fallback failed", q)
                status[q] = f"MISSING (bulk: {e}; API: {e2})"

    parents = fdic_parents()
    ust = treasury_yields()
    rows = build_rows(quarters, parents, ust)
    data = write_xlsx(rows)
    run_date = datetime.now(timezone.utc).date()
    fname = f"pipeline_data-2025Q2-2026Q3-{run_date.isoformat()}.xlsx"
    out = Path(a.out or fname)
    out.write_bytes(data)
    log.info("wrote %s: %d rows, %s bytes", out, len(rows), f"{len(data):,}")

    latest = next((f"{q.year}Q{(q.month - 1) // 3 + 1}" for q in reversed(QUARTERS) if q in quarters), None)
    cov = coverage(rows, latest) if latest else []
    lines = [f"call report pipeline field spec: Q3 2026 rerun -- built {run_date:%b %-d, %Y}", "",
             f"{len(rows):,} rows in tab pipeline_data, {len(HEADERS)} columns in Template order.", "",
             "Quarters:"]
    for q in QUARTERS:
        n = sum(1 for r in rows if r["YEAR PERIOD"] == f"{q.year}Q{(q.month - 1) // 3 + 1}")
        lines.append(f"  {q.year}Q{(q.month - 1) // 3 + 1}: {n:,} rows -- {status.get(q, 'not run')}")
    lines += ["", "Notes:",
              "  * Banks only (no FR Y-9C holding companies yet), as the spec allows.",
              "  * The existing 45 fields are filled for 2026Q2 and 2026Q3 only, by the same code as the current "
              "Q3 Pipeline Fields run; IDC-calculated columns stay blank.",
              "  * PARENT NUMBER / HOLD TYPE come from FDIC BankFind's current RSSDHCR (today's holding company, "
              "applied to every quarter)." + ("" if parents[0] else " BankFind did NOT respond this run: blank."),
              "  * FHLB borrowing capacity is blank (not on the Call Report).",
              "  * RCK_3360 and RC_2200 add foreign offices (RCFN) to domestic (RCON) for 031 filers.",
              "  * CBLR = RC-R Part I item 1 (RCOA LE74); where that is blank, Y if only a leverage ratio is "
              "reported.",
              "  * 2026Q3 filings were due Oct 30; late filers and amendments after today are not included.", ""]
    if cov:
        lines.append(f"Verify-flagged codes, banks with a value in {latest} (a code that comes back 0 / "
                     f"{cov[0][2]:,} is probably the wrong MDRM for that item):")
        for h, n, tot in cov:
            if h in VERIFY:
                lines.append(f"  {h}: {n:,} / {tot:,}")
        empty = [h for h, n, _t in cov if n == 0 and h not in pipeline_fields.IDC_ONLY
                 and h != "FHLB borrowing capacity"]
        if empty:
            lines += ["", f"Columns with no values at all in {latest}: " + ", ".join(empty)]
    text = "\n".join(lines)
    print(text)
    Path(str(out) + ".summary.txt").write_text(text)

    if a.no_email:
        return
    from ffiec_monitor import email_report
    from ffiec_monitor.config import EmailConfig
    import html as _html

    attach = [(fname, data, "vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
    if len(data) > 22_000_000:
        attach = []
        text += ("\n\nThe file is too large for an email attachment (%s bytes); download it from the GitHub "
                 "Actions run's artifacts (pipeline-data)." % f"{len(data):,}")
    email_report.send_email(
        EmailConfig.from_env(), f"Call report pipeline data 2025Q2-2026Q3 ({len(rows):,} rows)", text,
        html_body="<div style='font-family:Arial,sans-serif;font-size:13px;white-space:pre-wrap;'>"
                  + _html.escape(text) + "</div>",
        attachments=attach)
    log.info("emailed %s", fname)


if __name__ == "__main__":
    main()
