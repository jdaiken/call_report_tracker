"""Read-only cross-check: the monitor's stored Call Report ratios for one quarter vs. the FDIC's
own published figures (BankFind financials API), bank by bank, matched on FDIC cert.

Prints, per ratio, how many banks agree within tolerance and the worst disagreements. No email,
no DB writes.

  python scripts/verify_vs_fdic.py [PERIOD_END]     e.g. 2026-09-30 (default: latest stored)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path

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
]


def fdic(period: date) -> dict[str, dict]:
    fields = ",".join(["CERT", "NAME", "REPDTE"] + [c[2] for c in CHECKS])
    out, offset = {}, 0
    while True:
        r = requests.get("https://api.fdic.gov/banks/financials", timeout=180, params={
            "filters": f"REPDTE:{period:%Y%m%d}", "fields": fields, "limit": 10000, "offset": offset,
            "format": "json"})
        r.raise_for_status()
        rows = [d.get("data", d) for d in r.json().get("data", [])]
        for x in rows:
            out[str(x["CERT"]).lstrip("0")] = x
        if len(rows) < 10000:
            return out
        offset += 10000


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(period_arg: str | None = None) -> None:
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

    for label, key, field, tol in CHECKS:
        n = agree = 0
        bad = []
        for c in both:
            a, b = num(ours[c][2].get(key)), num(theirs[c].get(field))
            if a is None or b is None:
                continue
            n += 1
            ok = abs(a - b) <= (tol * max(abs(b), 1) if key == "total_assets" else tol)
            agree += ok
            if not ok:
                bad.append((abs(a - b), c, a, b))
        if not n:
            print(f"{label}: no comparable values (FDIC field {field} missing?)\n")
            continue
        print(f"{label} [{field}]: {agree}/{n} agree ({100 * agree / n:.1f}%), {len(bad)} differ")
        for _d, c, a, b in sorted(bad, reverse=True)[:8]:
            rssd, name, _r = ours[c]
            print(f"    {name[:42]:42} cert {c:>6} RSSD {rssd:>8}  ours {a:,.4g}  FDIC {b:,.4g}")
        print()


if __name__ == "__main__":
    main(*sys.argv[1:2])
