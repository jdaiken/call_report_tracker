"""Read-only cross-check: the monitor's stored Call Report ratios for one quarter vs. the FDIC's
own published figures (BankFind financials API), bank by bank, matched on FDIC cert.

Prints, per ratio, how many banks agree within tolerance and the worst disagreements. No email,
no DB writes.

  python scripts/verify_vs_fdic.py [PERIOD_END]     e.g. 2026-09-30 (default: latest stored)
  python scripts/verify_vs_fdic.py PERIOD_END --repull N

--repull re-pulls N of that quarter's Call Reports from FFIEC (the banks that disagreed worst, plus a
random sample) and compares ratios computed by the current code, so a formula fix can be checked
without waiting for the next filing. Needs FFIEC_USER_ID / FFIEC_TOKEN.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ffiec_monitor import db, ratios
from ffiec_monitor.config import FFIECConfig, get_duckdb_path
from ffiec_monitor.ffiec_client import FFIECClient
from ffiec_monitor.filings_workbook import extra_ratios

# (label, our ratios-dict key, FDIC field, tolerance). Ours and FDIC's are both percents, except
# total assets ($000 both). Tolerance is absolute percentage points (relative for assets).
CHECKS = [
    ("Total assets ($000)", "total_assets", "ASSET", 0.01),
    ("Tier 1 leverage", "tier1_leverage_ratio", "RBC1AAJ", 0.05),
    ("CET1", "cet1_ratio", "IDT1CER", 0.05),
    ("Tier 1 risk-based", "tier1_rbc_ratio", "RBC1RWAJ", 0.05),
    ("Total risk-based", "total_rbc_ratio", "RBCRWAJ", 0.05),
    ("ROA (annualized)", "roaa", "ROA", 0.05),
    ("ROE (annualized)", "roe", "ROE", 0.5),
    ("Net interest margin", "net_interest_margin", "NIMY", 0.10),
    ("Efficiency ratio", "efficiency_ratio", "EEFFR", 1.0),
    ("Noncurrent loans / loans", "noncurrent_to_loans", "NCLNLSR", 0.05),
    ("Net charge-off rate", "charge_off_ratio", "NTLNLSR", 0.05),
    ("Net loans / deposits", "net_loans_leases_to_deposits", "LNLSDEPR", 0.5),
    ("Core deposits / deposits", "core_deposits_to_total_deposits", "_COREDEP_PCT", 0.5),
]
EXTRA_FIELDS = ["COREDEP", "DEP"]


def fdic(period: date) -> dict[str, dict]:
    fields = ",".join(["CERT", "NAME", "REPDTE"] + [c[2] for c in CHECKS if not c[2].startswith("_")]
                      + EXTRA_FIELDS)
    out, offset = {}, 0
    while True:
        r = requests.get("https://api.fdic.gov/banks/financials", timeout=180, params={
            "filters": f"REPDTE:{period:%Y%m%d}", "fields": fields, "limit": 10000, "offset": offset,
            "format": "json"})
        r.raise_for_status()
        rows = [d.get("data", d) for d in r.json().get("data", [])]
        for x in rows:
            core, dep = num(x.get("COREDEP")), num(x.get("DEP"))
            x["_COREDEP_PCT"] = core / dep * 100 if core is not None and dep else None
            out[str(x["CERT"]).lstrip("0")] = x
        if len(rows) < 10000:
            return out
        offset += 10000


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(period_arg: str | None = None, *flags: str) -> None:
    repull = int(flags[flags.index("--repull") + 1]) if "--repull" in flags else 0
    conn = db.connect(get_duckdb_path())
    period = (date.fromisoformat(period_arg) if period_arg else
              conn.execute("SELECT max(period_end) FROM call_report_snapshots").fetchone()[0])
    ours = {}
    for rssd, raw in conn.execute("SELECT id_rssd, ratios FROM call_report_snapshots WHERE period_end = ?",
                                  [period]).fetchall():
        meta = db.get_institution_meta(conn, rssd)
        cert = (meta.fdic_cert or "").strip().lstrip("0") if meta else ""
        r = json.loads(raw)
        if r.get("total_assets") is None and meta:
            r["total_assets"] = meta.total_assets
        ours[cert or f"rssd:{rssd}"] = (rssd, meta.name.strip() if meta else rssd, r)

    theirs = fdic(period)
    print(f"Period {period}: {len(ours)} banks stored, {len(theirs)} in FDIC BankFind for that date")
    if not theirs:
        print("FDIC has not published this quarter yet -- nothing to compare.")
        return
    both = [c for c in ours if c in theirs]
    print(f"Matched on FDIC cert: {len(both)}; ours only: {len(ours) - len(both)}; FDIC only: "
          f"{len(set(theirs) - set(ours))}\n")
    if repull:
        both = _repull(ours, theirs, both, period, repull)
    compare(ours, theirs, both)


def _repull(ours, theirs, both, period, n):
    """Replace stored ratios with ones computed by the current code from freshly pulled reports."""
    worst = set()
    for _label, key, field, tol in CHECKS:
        diffs = sorted(((abs(num(ours[c][2].get(key)) - num(theirs[c].get(field))), c) for c in both
                        if num(ours[c][2].get(key)) is not None and num(theirs[c].get(field)) is not None),
                       reverse=True)
        worst.update(c for d, c in diffs[:12] if d > tol)
    rest = [c for c in both if c not in worst]
    random.seed(7)
    sample = sorted(worst) + random.sample(rest, max(0, min(len(rest), n - len(worst))))
    print(f"Re-pulling {len(sample)} reports ({len(worst)} worst disagreements + random sample)\n")
    client = FFIECClient(FFIECConfig.from_env())
    done, hk05 = [], 0
    for c in sample:
        rssd, name, _old = ours[c]
        try:
            fac = client.retrieve_facsimile(rssd, period)
        except Exception as e:
            print(f"    pull failed {name}: {e}")
            continue
        concepts = fac.get("concepts", fac)
        hk05 += "RCONHK05" in concepts and concepts["RCONHK05"] not in (None, "")
        r = {**ratios.extract_ratios(fac, period), **extra_ratios(concepts, period)}
        ours[c] = (rssd, name, r)
        done.append(c)
    print(f"Pulled {len(done)}; RCONHK05 (fully insured brokered) present in {hk05}\n")
    return done


def compare(ours, theirs, both):
    for label, key, field, tol in CHECKS:
        diffs, bad = [], []
        for c in both:
            a, b = num(ours[c][2].get(key)), num(theirs[c].get(field))
            if a is None or b is None:
                continue
            d = abs(a - b) / max(abs(b), 1) if key == "total_assets" else abs(a - b)
            diffs.append(d)
            if d > tol:
                bad.append((d, c, a, b))
        if not diffs:
            print(f"{label}: no comparable values (FDIC field {field} missing?)\n")
            continue
        n = len(diffs)
        wide = sum(d <= 5 * tol for d in diffs)
        print(f"{label} [{field}]: {n - len(bad)}/{n} within {tol:g} ({100 * (n - len(bad)) / n:.1f}%), "
              f"{100 * wide / n:.1f}% within {5 * tol:g}; median gap {statistics.median(diffs):.3g}")
        for _d, c, a, b in sorted(bad, reverse=True)[:8]:
            rssd, name, _r = ours[c]
            print(f"    {name[:42]:42} cert {c:>6} RSSD {rssd:>8}  ours {a:,.4g}  FDIC {b:,.4g}")
        print()


if __name__ == "__main__":
    main(*sys.argv[1:])
