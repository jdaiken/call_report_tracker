"""The 12 ratios of concern (spec §5), extraction from a UBPR facsimile, and diffing
against a prior snapshot with the §6 materiality floor applied.

All ratio values are stored/compared as percentage points (e.g. 8.23 means 8.23%).

NOTE on field mapping: `UBPR_FIELD_MAP` below maps our ratio keys to UBPR XBRL
concept names. These have not been validated against a live `RetrieveUBPRXBRLFacsimile`
response (no token available in this build environment) -- treat them as the seam to
correct first if a real pull comes back with different tag names. Everything else in
this module (diffing, materiality floor, composite handling) is independent of that
mapping and does not need to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# 5bp floor for ratio (percentage-point) revisions -- spec §6 Tier 3.
RATIO_MATERIALITY_FLOOR_PP = 0.05
# ~1% relative floor for dollar figures (e.g. total assets) -- spec §6 Tier 3.
DOLLAR_MATERIALITY_RELATIVE_FLOOR = 0.01


@dataclass(frozen=True)
class RatioSpec:
    key: str
    label: str
    category: str
    peer_key: Optional[str] = None       # e.g. charge-off ratio's peer average
    paired_key: Optional[str] = None     # e.g. loan growth's paired capital growth


# Order matches the spec §5 table exactly -- this drives display order everywhere.
RATIO_SPECS: list[RatioSpec] = [
    RatioSpec("tier1_leverage_ratio", "Tier 1 Leverage Ratio", "Capital"),
    RatioSpec("tier1_rbc_ratio", "Tier 1 Risk-Based Capital Ratio", "Capital"),
    RatioSpec("total_rbc_ratio", "Total Risk-Based Capital Ratio", "Capital"),
    RatioSpec("net_noncore_funding_dependence_ratio", "Net Non-Core Funding Dependence Ratio", "Liquidity"),
    RatioSpec("net_loans_leases_to_deposits", "Net Loans & Leases to Deposits", "Liquidity"),
    RatioSpec("pledged_securities_ratio", "Pledged Securities Ratio", "Liquidity"),
    RatioSpec(
        "charge_off_ratio", "Charge-off Ratio vs. Peer Average", "Asset Quality",
        peer_key="peer_avg_charge_off_ratio",
    ),
    RatioSpec(
        "loan_growth_rate", "Loan Growth vs. Capital Growth", "Asset Quality",
        paired_key="capital_growth_rate",
    ),
    RatioSpec("roaa", "ROAA", "Earnings"),
    RatioSpec("net_interest_margin", "Net Interest Margin", "Earnings"),
    RatioSpec("core_deposits_to_total_deposits", "Core Deposits to Total Deposits", "Deposit Composition"),
    RatioSpec(
        "brokered_reciprocal_deposits_to_total_deposits",
        "Brokered/Reciprocal Deposits to Total Deposits", "Deposit Composition",
    ),
]

# CET1 is needed for Watch List LTWC determination (spec §4) but is NOT one of the 12
# ratios of concern shown in the report (spec §5's table only has 3 capital ratios).
CET1_KEY = "cet1_ratio"

# ratio key -> UBPR XBRL concept name. Placeholder/best-effort; see module docstring.
UBPR_FIELD_MAP: dict[str, str] = {
    "tier1_leverage_ratio": "UBPRE011",
    "tier1_rbc_ratio": "UBPRE012",
    "total_rbc_ratio": "UBPRE013",
    "cet1_ratio": "UBPRE014",
    "net_noncore_funding_dependence_ratio": "UBPRK454",
    "net_loans_leases_to_deposits": "UBPRK462",
    "pledged_securities_ratio": "UBPRK423",
    "charge_off_ratio": "UBPRD657",
    "peer_avg_charge_off_ratio": "UBPRDP57",
    "loan_growth_rate": "UBPRC109",
    "capital_growth_rate": "UBPRC110",
    "roaa": "UBPR7401",
    "net_interest_margin": "UBPR7403",
    "core_deposits_to_total_deposits": "UBPRK449",
    "brokered_reciprocal_deposits_to_total_deposits": "UBPRK450",
    # Not one of the 12 ratios of concern shown in every email, but needed to drive
    # the Tier 1 "noncurrent loans+OREO doubles QoQ / crosses ~3%" trigger (spec §6).
    "noncurrent_loans_oreo_ratio": "UBPRD772",
}

# Call Report (not UBPR) concepts, keyed by the real MDRM item codes. RCON is used by
# banks with no foreign offices; RCFD by banks that must consolidate foreign offices.
TOTAL_ASSETS_TAGS = ("RCON2170", "RCFD2170")


def extract_total_assets(facsimile: dict[str, Any]) -> Optional[float]:
    """Total assets from a Call Report facsimile -- used for the email identity
    block (spec §7) and the Tier 2 asset-growth trigger (spec §6)."""
    concepts = facsimile.get("concepts", facsimile)
    for tag in TOTAL_ASSETS_TAGS:
        value = _to_float(concepts.get(tag))
        if value is not None:
            return value
    return None


def extract_ratios(ubpr_facsimile: dict[str, Any]) -> dict[str, Optional[float]]:
    """Pull every tracked ratio (the 12 + CET1) out of a raw UBPR facsimile payload.

    Missing/unparseable fields come back as None rather than raising -- a bank with a
    gap in one ratio shouldn't block diffing every other ratio.
    """
    concepts = ubpr_facsimile.get("concepts", ubpr_facsimile)
    out: dict[str, Optional[float]] = {}
    for key, concept in UBPR_FIELD_MAP.items():
        raw = concepts.get(concept)
        out[key] = _to_float(raw)
    return out


def _to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RatioChange:
    key: str
    label: str
    category: str
    old_value: Optional[float]
    new_value: Optional[float]
    material: bool
    peer_value: Optional[float] = None          # current peer average, charge-off only
    paired: Optional["RatioChange"] = None       # capital growth, nested under loan growth


def _is_material(old: Optional[float], new: Optional[float]) -> bool:
    if old is None or new is None:
        # A ratio that newly appears (or disappears) is always worth surfacing.
        return old != new
    return abs(new - old) >= RATIO_MATERIALITY_FLOOR_PP


def is_material_dollar_change(old: Optional[float], new: Optional[float]) -> bool:
    if old is None or new is None:
        return old != new
    if old == 0:
        return new != 0
    return abs(new - old) / abs(old) >= DOLLAR_MATERIALITY_RELATIVE_FLOOR


def diff_ratios(old: dict[str, Optional[float]], new: dict[str, Optional[float]]) -> list[RatioChange]:
    """Old-vs-new diff for all 12 ratios of concern, materiality floor applied.

    Returns only ratios whose change clears the floor (spec §6 Tier 3: sub-floor
    revisions are noise, not something Tier 1/2 logic -- or the email -- should see).
    Special-cased per spec §5: charge-off carries its peer average as context (no
    old->new needed for the peer figure itself); loan growth carries capital growth
    as a paired, separately-diffed row rather than one combined line.
    """
    changes: list[RatioChange] = []
    for spec in RATIO_SPECS:
        old_v, new_v = old.get(spec.key), new.get(spec.key)
        material = _is_material(old_v, new_v)

        paired_change = None
        if spec.paired_key:
            p_old, p_new = old.get(spec.paired_key), new.get(spec.paired_key)
            p_material = _is_material(p_old, p_new)
            if p_material:
                paired_change = RatioChange(
                    key=spec.paired_key,
                    label="Capital Growth",
                    category=spec.category,
                    old_value=p_old,
                    new_value=p_new,
                    material=True,
                )
            material = material or p_material

        if not material:
            continue

        changes.append(
            RatioChange(
                key=spec.key,
                label=spec.label,
                category=spec.category,
                old_value=old_v,
                new_value=new_v,
                material=True,
                peer_value=new.get(spec.peer_key) if spec.peer_key else None,
                paired=paired_change,
            )
        )
    return changes


def full_snapshot_rows(ratios: dict[str, Optional[float]]) -> list[RatioChange]:
    """Every one of the 12 ratios at current value, for the Watch List standing
    snapshot (spec §7.3) -- shown daily regardless of whether anything changed."""
    rows = []
    for spec in RATIO_SPECS:
        paired = None
        if spec.paired_key:
            paired = RatioChange(
                key=spec.paired_key, label="Capital Growth", category=spec.category,
                old_value=None, new_value=ratios.get(spec.paired_key), material=True,
            )
        rows.append(
            RatioChange(
                key=spec.key,
                label=spec.label,
                category=spec.category,
                old_value=None,
                new_value=ratios.get(spec.key),
                material=True,
                peer_value=ratios.get(spec.peer_key) if spec.peer_key else None,
                paired=paired,
            )
        )
    return rows
