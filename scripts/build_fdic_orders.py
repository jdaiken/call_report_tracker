"""Build data/fdic_active_orders.csv from an export of the FDIC's Enforcement Decisions
and Orders search (https://orders.fdic.gov/s/ -> export CSV, e.g. EDOOrders.csv).

Keeps institution-level (respondent = the bank) consent / cease-and-desist orders, written
agreements, PCA and capital directives and safety-and-soundness orders issued since 2016,
dropping any whose docket later shows a termination, withdrawal or dismissal. Civil money
penalties and orders against individuals are left out.

  python scripts/build_fdic_orders.py path/to/EDOOrders.csv "Oct 26, 2026"
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "fdic_active_orders.csv"
SINCE = "2016"
INST_TYPES = ("Cease and Desist / Consent Orders", "Written Agreement", "Prompt Corrective Action", "Capital Directive",
              "Safety and Soundness Order", "Temporary Order to Cease and Desist", "Order to Cease and Desist")
CLOSED = ("Termination", "Withdrawal", "Dismissal", "Denial")


def _split(v: str) -> list[str]:
    return [p.strip() for p in v.split(";") if p.strip()]


def _dockets(v: str) -> set[str]:
    return {re.sub(r"\s+", "", d).upper() for d in re.split(r"[,;]", v) if d.strip() and "NO DOCKET" not in d.upper()}


def build(src: str) -> list[dict]:
    with open(src, encoding="utf-8-sig", errors="replace") as fh:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]
    closed = set()
    for r in rows:
        if any(c in r["Category"] for c in CLOSED[:3]):
            closed |= _dockets(r["Docket Number"])
    out = []
    for r in rows:
        if not any(t in INST_TYPES for t in _split(r["Action Type"])) or any(c in r["Category"] for c in CLOSED):
            continue
        if r["Termination Date"] or r["Issued Date"][:4] < SINCE or (_dockets(r["Docket Number"]) & closed):
            continue
        resp, banks, certs = _split(r["Respondent"]), _split(r["Bank Name"]), _split(r["Cert Number"])
        if not banks or not certs or certs[0] == "N/A" or not any(a.lower() == b.lower() for a in resp for b in banks):
            continue                                   # an individual, not the institution
        city, state = _split(r["Bank City"]), _split(r["Bank State"])
        out.append({"cert": certs[0], "bank": banks[0], "city": city[0] if city else "", "state": state[0] if state else "",
                    "action": r["Action Type"], "title": r["Order Title"], "issued": r["Issued Date"],
                    "docket": r["Docket Number"], "url": r["File URL"]})
    return out


if __name__ == "__main__":
    rows = build(sys.argv[1])
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    OUT.with_suffix(".asof").write_text((sys.argv[2] if len(sys.argv) > 2 else "") + "\n")
    print(f"{len(rows)} active FDIC orders -> {OUT}")
