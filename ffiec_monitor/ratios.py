"""The 12 ratios of concern (spec §5): extraction directly from a Call Report
facsimile (the live `RetrieveFacsimile` daily pull, spec §1/§2), and diffing
against a prior snapshot with the §6 materiality floor applied.

All ratio values are stored/compared as percentage points (e.g. 8.23 means 8.23%).

Field mapping confidence: the MDRM codes below were verified against a real FFIEC
CDR bulk Call Report download (period 2026-06-30, ~4,300 institutions) -- not
guessed. Confidence varies by ratio, noted inline:
  - Capital ratios (leverage, Tier 1 RBC, Total RBC, CET1): HIGH -- read directly
    off Schedule RC-R Part I, no derivation.
  - Loans/deposits, pledged securities, core/brokered+reciprocal deposits,
    noncurrent+OREO: HIGH -- standard ratios of two directly-reported balances.
  - ROAA, NIM, charge-off ratio: MEDIUM -- correct components, but annualize a
    single period's YTD income against that period's own average balance rather
    than a true multi-quarter YTD average (Call Report doesn't publish one).
  - Net Non-Core Funding Dependence: MEDIUM-LOW -- a simplified stand-in for
    UBPR's maturity-bucketed formula; directionally useful, not bit-exact.
  - Peer average charge-off: NOT computed (returns None) -- needs FFIEC peer
    group definitions and aggregation across many institutions; out of scope here.
  - Loan/capital growth: computed in `compute_growth_rates`, not `extract_ratios`,
    since growth inherently needs the *prior* period's raw balances (see below).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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

RECIPROCAL_RAW_KEY = "_reciprocal_raw"
LIABILITIES_RAW_KEY = "_total_liabilities_raw"
UNINSURED_RAW_KEY = "_uninsured_raw"
DEPOSITS_RAW_KEY = "_total_deposits_raw"
RECIPROCAL_CAP_DOLLARS_000 = 5_000_000          # $5 billion, in $000
NETWORK_KEYS = (RECIPROCAL_RAW_KEY, LIABILITIES_RAW_KEY, UNINSURED_RAW_KEY, DEPOSITS_RAW_KEY)

# CET1 is needed for Watch List LTWC determination (spec §4) but is NOT one of the 12
# ratios of concern shown in the report (spec §5's table only has 3 capital ratios).
CET1_KEY = "cet1_ratio"

# Raw dollar figures carried inside the ratios dict alongside the 12 ratios, purely
# so the NEXT period's pipeline run can compute loan/capital growth (see
# compute_growth_rates). Underscore-prefixed so they're plainly "not a displayed
# ratio" -- RATIO_SPECS never references them, so diff_ratios/full_snapshot_rows
# ignore them automatically.
_NET_LOANS_RAW_KEY = "_net_loans_raw"
_TOTAL_EQUITY_RAW_KEY = "_total_equity_raw"


def annualization_factor(period_end: date) -> float:
    """Call Report income-statement (RIAD*) items are YTD as of period_end;
    multiply by this to annualize. Q1->x4, Q2->x2, Q3->x4/3, Q4->x1."""
    quarters_elapsed = period_end.month // 3
    return 4.0 / quarters_elapsed


def _num(raw: Any) -> Optional[float]:
    """Parse one Call Report field: plain integers for dollar amounts (reported in
    thousands), or a "12.3456%" string for ratio fields -- verified against a real
    bulk download, both formats occur in the same file."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


_to_float = _num  # kept as an alias; other modules may still import this name


def _pick(concepts: dict[str, Any], *codes: str) -> Optional[float]:
    """First code with a parseable value. Verified against the bulk file that
    RCON/RCFD (and RCOA/RCFA capital-ratio) pairs are mutually exclusive per
    institution in practice -- e.g. RCON2170 and RCFD2170 are never both populated
    for the same bank -- so a fixed try-order is safe, not a guess about precedence.
    """
    for code in codes:
        value = _num(concepts.get(code))
        if value is not None:
            return value
    return None


def _sum_present(concepts: dict[str, Any], *codes: str) -> Optional[float]:
    """Sum whichever of these codes are present, treating a missing/blank code as
    0 -- appropriate for category breakdowns (e.g. a bank with no farm loans just
    doesn't populate that charge-off line). Returns None only if NONE are present,
    so a schedule that's entirely absent doesn't silently become a false zero."""
    values = [_num(concepts.get(code)) for code in codes]
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _ratio_pct(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator * 100


def extract_total_assets(facsimile: dict[str, Any]) -> Optional[float]:
    """Total assets (in thousands of dollars) -- spec §7 identity block, §6 asset-
    growth trigger. RCFD used by consolidated (foreign-office) filers, RCON by
    everyone else -- verified mutually exclusive in the bulk data."""
    concepts = facsimile.get("concepts", facsimile)
    return _pick(concepts, "RCFD2170", "RCON2170")


def extract_ratios(facsimile: dict[str, Any], period_end: date) -> dict[str, Optional[float]]:
    """Compute the 12 ratios of concern (+ CET1, + noncurrent/OREO) from a single
    Call Report facsimile. See module docstring for per-ratio confidence.

    Missing/unparseable inputs propagate as None for that ratio rather than raising
    -- a bank with a gap in one figure shouldn't block diffing every other ratio.
    Loan/capital growth are NOT set here (both None) -- see compute_growth_rates.
    """
    c = facsimile.get("concepts", facsimile)
    out: dict[str, Optional[float]] = {}

    # --- Capital: reported directly on Schedule RC-R Part I. RCOA is the column
    # ~98% of banks use; RCFA is a second column used by a small (~2%) advanced-
    # approaches subset -- verified counts, not a guessed precedence. A Community
    # Bank Leverage Ratio (CBLR) bank legitimately reports ONLY the leverage ratio
    # and leaves the other three blank -- that's real data, not a parsing gap.
    out["tier1_leverage_ratio"] = _pick(c, "RCOA7204", "RCFA7204")
    out["tier1_rbc_ratio"] = _pick(c, "RCOA7206", "RCFA7206")
    out["total_rbc_ratio"] = _pick(c, "RCOA7205", "RCFA7205")
    out[CET1_KEY] = _pick(c, "RCOAP793", "RCFAP793")

    total_assets = extract_total_assets(facsimile)
    net_loans = _pick(c, "RCFDB529", "RCONB529")  # loans & leases, net of unearned income & ACL
    domestic_deposits = _pick(c, "RCON2200")
    foreign_deposits = _pick(c, "RCFN2200") or 0.0
    total_deposits = (domestic_deposits + foreign_deposits) if domestic_deposits is not None else None

    out[_NET_LOANS_RAW_KEY] = net_loans
    out[_TOTAL_EQUITY_RAW_KEY] = _pick(c, "RCFD3210", "RCON3210")

    out["net_loans_leases_to_deposits"] = _ratio_pct(net_loans, total_deposits)

    # Pledged securities / total securities (AFS + HTM). Excludes equity securities
    # at fair value, which are typically immaterial for this ratio.
    pledged = _pick(c, "RCFD0416", "RCON0416")
    afs = _pick(c, "RCFD1773", "RCON1773") or 0.0
    htm = _pick(c, "RCFDJJ34", "RCONJJ34") or 0.0
    out["pledged_securities_ratio"] = _ratio_pct(pledged, afs + htm) if (afs + htm) else None

    brokered = _pick(c, "RCON2365") or 0.0
    time_gt_250k = _pick(c, "RCONJ474") or 0.0
    reciprocal = _pick(c, "RCONJH83") or 0.0

    # Net Non-Core Funding Dependence -- simplified approximation (see module
    # docstring): (non-core liabilities - short-term investments) / long-term
    # assets, "long-term" approximated as total assets minus short-term investments
    # rather than a true maturity-based split.
    other_borrowed = _pick(c, "RCFD3190", "RCON3190") or 0.0
    repo_liabilities = _pick(c, "RCFDB995", "RCONB995") or 0.0
    non_core_liabilities = brokered + time_gt_250k + other_borrowed + repo_liabilities + foreign_deposits
    short_term_investments = (_pick(c, "RCFDB989", "RCONB989") or 0.0) + (_pick(c, "RCFD0071", "RCON0071") or 0.0)
    if total_assets is not None:
        long_term_assets = total_assets - short_term_investments
        out["net_noncore_funding_dependence_ratio"] = (
            _ratio_pct(non_core_liabilities - short_term_investments, long_term_assets)
            if long_term_assets else None
        )
    else:
        out["net_noncore_funding_dependence_ratio"] = None

    out["brokered_reciprocal_deposits_to_total_deposits"] = _ratio_pct(brokered + reciprocal, total_deposits)

    # Network exposure (ffiec_monitor/network.py). Reciprocal deposits (RC-E memo, JH83) can be
    # excepted from "brokered" up to the lesser of $5 billion or 20% of total liabilities (12 CFR
    # 337.6(e)); uninsured deposits = RC-O memo item 2 estimate (5597). Raw figures in $000.
    total_liabilities = _pick(c, "RCFD2948", "RCON2948")
    uninsured = _pick(c, "RCON5597")
    out[RECIPROCAL_RAW_KEY] = _pick(c, "RCONJH83")
    out[LIABILITIES_RAW_KEY] = total_liabilities
    out[UNINSURED_RAW_KEY] = uninsured
    out[DEPOSITS_RAW_KEY] = total_deposits
    cap = min(RECIPROCAL_CAP_DOLLARS_000, 0.20 * total_liabilities) if total_liabilities else None
    out["reciprocal_cap_used"] = _ratio_pct(out[RECIPROCAL_RAW_KEY], cap) if out[RECIPROCAL_RAW_KEY] is not None else None
    out["uninsured_deposit_share"] = _ratio_pct(uninsured, total_deposits)

    if total_deposits is not None:
        core_deposits = total_deposits - brokered - time_gt_250k
        out["core_deposits_to_total_deposits"] = _ratio_pct(core_deposits, total_deposits)
    else:
        out["core_deposits_to_total_deposits"] = None

    # Noncurrent (nonaccrual + 90+ days past due) loans + OREO, over total loans + OREO.
    nonaccrual = _pick(c, "RCFD1403", "RCON1403") or 0.0
    past_due_90 = _pick(c, "RCFD1407", "RCON1407") or 0.0
    oreo = _pick(c, "RCFD2150", "RCON2150") or 0.0
    out["noncurrent_loans_oreo_ratio"] = _ratio_pct(nonaccrual + past_due_90 + oreo, net_loans + oreo) \
        if net_loans is not None else None

    factor = annualization_factor(period_end)

    # ROAA: YTD net income, annualized, over the current quarter's own average
    # assets (RC-K) -- a single-period approximation of a true YTD average.
    net_income = _pick(c, "RIAD4340")
    avg_assets = _pick(c, "RCFD3368", "RCON3368")
    out["roaa"] = _ratio_pct(net_income * factor, avg_assets) if net_income is not None else None

    # NIM: average earning assets approximated as avg loans + avg securities + avg
    # fed funds sold/resell + avg due-from-banks + avg trading assets (RC-K) --
    # Call Report has no single published "earning assets" total to read directly.
    net_interest_income = _pick(c, "RIAD4074")
    avg_loans = _sum_present(c, "RCON3360", "RCFN3360")
    avg_earning_assets = _sum_present(
        c, "RCON3360", "RCFN3360", "RCONB558", "RCONB559", "RCONB560", "RCON3365", "RCON3381", "RCON3401",
    )
    out["net_interest_margin"] = _ratio_pct(net_interest_income * factor, avg_earning_assets) \
        if net_interest_income is not None else None

    # Charge-off ratio: sums the charge-off/recovery category lines present in
    # Schedule RI-B Part I. Approximation -- see module docstring.
    charge_offs = _sum_present(c, "RIAD4638", "RIAD4655", "RIADB514", "RIADC880", "RIADC891", "RIADK205")
    recoveries = _sum_present(c, "RIAD4608", "RIAD4665", "RIADB515", "RIADF188", "RIADC892", "RIADK206")
    if charge_offs is None and recoveries is None:
        out["charge_off_ratio"] = None
    else:
        net_charge_offs = (charge_offs or 0.0) - (recoveries or 0.0)
        out["charge_off_ratio"] = _ratio_pct(net_charge_offs * factor, avg_loans)

    # Not computed -- needs FFIEC peer group definitions + cross-institution
    # aggregation, out of scope for a single facsimile extraction.
    out["peer_avg_charge_off_ratio"] = None

    # Growth rates need the *prior* period's raw balances -- see compute_growth_rates.
    out["loan_growth_rate"] = None
    out["capital_growth_rate"] = None

    return out


def compute_growth_rates(
    prior: dict[str, Optional[float]],
    current: dict[str, Optional[float]],
    prior_period_end: date,
    current_period_end: date,
) -> None:
    """Fills in current['loan_growth_rate'] / current['capital_growth_rate'] using
    the raw dollar balances extract_ratios stashed in both snapshots, annualized
    over however many months actually elapsed (robust to a skipped quarter, not
    just a strict QoQ assumption). Mutates `current` in place; no-ops (leaves None)
    when prior data isn't available -- e.g. the very first period seen for a bank.
    """
    months = (current_period_end.year - prior_period_end.year) * 12 + (
        current_period_end.month - prior_period_end.month
    )
    if months <= 0:
        return
    annualize = 12.0 / months

    for raw_key, out_key in ((_NET_LOANS_RAW_KEY, "loan_growth_rate"), (_TOTAL_EQUITY_RAW_KEY, "capital_growth_rate")):
        old_v, new_v = prior.get(raw_key), current.get(raw_key)
        if old_v is None or new_v is None or old_v == 0:
            continue
        current[out_key] = ((new_v - old_v) / old_v) * annualize * 100


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
