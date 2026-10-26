"""Quarter-over-quarter performance for the banks that matter most in a report: banks
under an active enforcement order, banks below well capitalized, and community bank
leverage ratio banks under 9%.

For each: the key ratios this quarter vs. last quarter, and a plain-English read built
from rules (capital, earnings, asset quality, liquidity, brokered/reciprocal funding).
Last quarter comes from `prior_lookup(rssd, prior_quarter_end)`, which the caller
supplies (stored snapshot, else a fresh Call Report pull).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

# (key, label, kind, "good" direction): kind pct = percent; direction +1 higher is better, -1 lower is better
METRICS = [
    ("tier1_leverage_ratio", "Leverage ratio", +1),
    ("total_rbc_ratio", "Total risk-based capital", +1),
    ("roaa", "Return on assets", +1),
    ("net_interest_margin", "Net interest margin", +1),
    ("noncurrent_loans_oreo_ratio", "Noncurrent loans + OREO", -1),
    ("net_loans_leases_to_deposits", "Loans to deposits", -1),
    ("core_deposits_to_total_deposits", "Core deposits share", +1),
    ("brokered_reciprocal_deposits_to_total_deposits", "Brokered/reciprocal share", -1),
    ("net_noncore_funding_dependence_ratio", "Non-core funding dependence", -1),
]


def prior_quarter_end(p: date) -> date:
    m = p.month - 3
    if m <= 0:
        return date(p.year - 1, 12, 31)
    return date(p.year, m, 30 if m in (6, 9) else 31)


@dataclass
class Row:
    label: str
    prior: Optional[float]
    current: Optional[float]
    better: Optional[bool]      # did it move the good way (None = no change data / flat)

    @property
    def change(self) -> Optional[float]:
        return None if self.prior is None or self.current is None else self.current - self.prior


@dataclass
class BankPerformance:
    bank: object                 # report.Bank
    why: list[str]               # why it's in this section (order, below well capitalized, CBLR < 9%)
    prior_label: str
    rows: list[Row] = field(default_factory=list)
    read: str = ""


def _num(r: Optional[dict], k: str) -> Optional[float]:
    v = (r or {}).get(k)
    return float(v) if isinstance(v, (int, float)) else None


def _read(cur: dict, rows: list[Row]) -> str:
    """One plain-English line: what's improving, what's worsening, what stands out."""
    by = {r.label: r for r in rows}
    bits = []
    lev = by["Leverage ratio"]
    if lev.change is not None and abs(lev.change) >= 0.25:
        bits.append(f"capital {'up' if lev.change > 0 else 'down'} {abs(lev.change):.2f} pts")
    roa = _num(cur, "roaa")
    if roa is not None:
        bits.append("losing money" if roa < 0 else "weak earnings" if roa < 0.4 else "profitable")
    nc = by["Noncurrent loans + OREO"]
    if nc.current is not None and (nc.current >= 2 or (nc.change or 0) >= 0.5):
        bits.append(f"problem loans {nc.current:.2f}%" + (f" (up {nc.change:.2f})" if (nc.change or 0) >= 0.25 else ""))
    ltd = by["Loans to deposits"]
    if ltd.current is not None and ltd.current > 100:
        bits.append(f"loans {ltd.current:.0f}% of deposits")
    br = by["Brokered/reciprocal share"]
    if br.current is not None and (br.current >= 15 or (br.change or 0) >= 3):
        bits.append(f"brokered/reciprocal {br.current:.1f}% of deposits" + (f" (up {br.change:.1f})" if (br.change or 0) >= 1 else ""))
    core = by["Core deposits share"]
    if core.change is not None and core.change <= -3:
        bits.append(f"core deposits down {abs(core.change):.1f} pts")
    improving = sum(1 for r in rows if r.better is True)
    worsening = sum(1 for r in rows if r.better is False)
    trend = ("mostly improving" if improving > worsening + 1 else "mostly worsening" if worsening > improving + 1
             else "mixed") if any(r.change is not None for r in rows) else "no prior quarter to compare"
    return (trend.capitalize() + (": " + ", ".join(bits) if bits else "") + ".")


def make_prior_lookup(conn, client):
    """Last quarter's ratios: the stored snapshot if it has data, else a fresh Call Report pull."""
    from . import db, ratios as ratios_mod

    def lookup(rssd: str, period_end: date) -> Optional[dict]:
        snap = db.get_snapshot(conn, rssd, period_end)
        if snap is not None and any(isinstance(v, (int, float)) for v in snap.ratios.values()):
            return snap.ratios
        if client is None:
            return None
        return ratios_mod.extract_ratios(client.retrieve_facsimile(rssd, period_end), period_end)
    return lookup


def build(focus: list[tuple[object, list[str], dict, date]],
          prior_lookup: Optional[Callable[[str, date], Optional[dict]]]) -> list[BankPerformance]:
    """focus: (Bank, why, current ratios, period_end) -- one per bank."""
    out = []
    for bank, why, cur, period_end in focus:
        pq = prior_quarter_end(period_end)
        prior = None
        if prior_lookup is not None:
            try:
                prior = prior_lookup(bank.rssd, pq)
            except Exception:
                prior = None
        rows = []
        for key, label, good in METRICS:
            c, p = _num(cur, key), _num(prior, key)
            better = None
            if c is not None and p is not None and abs(c - p) >= 0.05:
                better = (c > p) == (good > 0)
            rows.append(Row(label, p, c, better))
        bp = BankPerformance(bank, why, f"Q{(pq.month - 1) // 3 + 1} {pq.year}", rows)
        bp.read = _read(cur, rows)
        out.append(bp)
    return out
