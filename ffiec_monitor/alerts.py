"""Alert tier classification -- spec §6.

Two distinct situations feed this module (spec §2/§6):
  1. A genuinely new quarter filed (QoQ comparison against the prior period).
  2. A refiling/restatement of a period we'd already seen (compare stored-vs-refiled
     values for the *same* period_end) -- the "quiet restatement" case, which only
     has one rule: a capital ratio restated down >50bp is Tier 1, full stop.

Both produce Trigger objects; the Watch List escalation rule (any Tier 2 trigger on
a bank already on the Watch List becomes Tier 1) is applied afterward by the caller
via `escalate_for_watch_list`, since it needs Watch List membership which this module
doesn't know about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ratios import RatioChange, diff_ratios, is_material_dollar_change

TIER1 = "TIER1"
TIER2 = "TIER2"

# PCA tier boundaries banks can cross downward through (spec §6).
LEVERAGE_PCA_BOUNDARIES = (5.0, 4.0, 3.0)
TOTAL_RBC_PCA_BOUNDARIES = (10.0, 8.0, 6.0)

CAPITAL_RATIO_KEYS = ("tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio", "cet1_ratio")
RESTATEMENT_THRESHOLD_PP = 0.50  # >50bp down on a capital ratio, on an already-seen period

NONCURRENT_OREO_ABSOLUTE_CROSS = 3.0
LEVERAGE_OR_RBC_TIER2_DROP_PP = 1.00  # >100bp QoQ, spec §6 Tier 2
ROAA_TIER2_FLOOR = 0.50
ROAA_TIER2_CONSECUTIVE_QUARTERS = 2
ASSET_GROWTH_TIER2_FLOOR_PCT = -10.0
LOANS_DEPOSITS_SHARP_INCREASE_PP = 5.0  # "up sharply QoQ" -- treated as a >=5pp QoQ jump


@dataclass
class Trigger:
    tier: str
    trigger_type: str
    description: str
    ratio_changes: list[RatioChange] = field(default_factory=list)
    escalated: bool = False


def _crossings(old: Optional[float], new: Optional[float], boundaries: tuple[float, ...]) -> list[float]:
    if old is None or new is None:
        return []
    return [b for b in boundaries if old >= b > new]


def classify_new_period(
    prior: dict[str, Optional[float]],
    current: dict[str, Optional[float]],
    roaa_history: list[Optional[float]],
    prior_total_assets: Optional[float],
    current_total_assets: Optional[float],
) -> list[Trigger]:
    """QoQ classification for a genuinely new reporting period.

    `roaa_history` is prior consecutive quarters' ROAA, oldest first, NOT including
    the current period -- used for the "below 0.50% for 2+ consecutive quarters"
    Tier 2 rule.
    """
    triggers: list[Trigger] = []
    changes = {c.key: c for c in diff_ratios(prior, current)}

    def change_for(key: str) -> Optional[RatioChange]:
        return changes.get(key)

    # --- Tier 1 ---------------------------------------------------------

    leverage_old, leverage_new = prior.get("tier1_leverage_ratio"), current.get("tier1_leverage_ratio")
    for boundary in _crossings(leverage_old, leverage_new, LEVERAGE_PCA_BOUNDARIES):
        triggers.append(Trigger(
            TIER1, "LEVERAGE_PCA_CROSSING",
            f"Tier 1 Leverage Ratio crossed below {boundary:g}% "
            f"({leverage_old:.2f}% -> {leverage_new:.2f}%)",
            ratio_changes=[c for c in [change_for("tier1_leverage_ratio")] if c],
        ))

    rbc_old, rbc_new = prior.get("total_rbc_ratio"), current.get("total_rbc_ratio")
    for boundary in _crossings(rbc_old, rbc_new, TOTAL_RBC_PCA_BOUNDARIES):
        triggers.append(Trigger(
            TIER1, "TOTAL_RBC_PCA_CROSSING",
            f"Total Risk-Based Capital Ratio crossed below {boundary:g}% "
            f"({rbc_old:.2f}% -> {rbc_new:.2f}%)",
            ratio_changes=[c for c in [change_for("total_rbc_ratio")] if c],
        ))

    roaa_old, roaa_new = prior.get("roaa"), current.get("roaa")
    if roaa_new is not None and roaa_new < 0 and (roaa_old is None or roaa_old >= 0):
        triggers.append(Trigger(
            TIER1, "ROAA_NEGATIVE",
            f"ROAA went negative for the quarter ({_fmt(roaa_old)} -> {roaa_new:.2f}%)",
            ratio_changes=[c for c in [change_for("roaa")] if c],
        ))

    nco_old, nco_new = prior.get("noncurrent_loans_oreo_ratio"), current.get("noncurrent_loans_oreo_ratio")
    if nco_new is not None:
        doubled = nco_old is not None and nco_old > 0 and nco_new >= 2 * nco_old
        crossed_absolute = nco_old is not None and nco_old < NONCURRENT_OREO_ABSOLUTE_CROSS <= nco_new
        if doubled or crossed_absolute:
            reason = "doubled QoQ" if doubled else f"crossed {NONCURRENT_OREO_ABSOLUTE_CROSS:g}%"
            triggers.append(Trigger(
                TIER1, "NONCURRENT_OREO_STRESS",
                f"Noncurrent loans+OREO ratio {reason} ({_fmt(nco_old)} -> {nco_new:.2f}%)",
            ))

    # --- Tier 2 -----------------------------------------------------------

    if leverage_old is not None and leverage_new is not None:
        drop = leverage_old - leverage_new
        if drop >= LEVERAGE_OR_RBC_TIER2_DROP_PP and not _crossings(leverage_old, leverage_new, LEVERAGE_PCA_BOUNDARIES):
            triggers.append(Trigger(
                TIER2, "LEVERAGE_DOWN_100BP",
                f"Tier 1 Leverage Ratio down {drop:.2f}pp QoQ, still above well-capitalized "
                f"line ({leverage_old:.2f}% -> {leverage_new:.2f}%)",
                ratio_changes=[c for c in [change_for("tier1_leverage_ratio")] if c],
            ))

    if rbc_old is not None and rbc_new is not None:
        drop = rbc_old - rbc_new
        if drop >= LEVERAGE_OR_RBC_TIER2_DROP_PP and not _crossings(rbc_old, rbc_new, TOTAL_RBC_PCA_BOUNDARIES):
            triggers.append(Trigger(
                TIER2, "TOTAL_RBC_DOWN_100BP",
                f"Total Risk-Based Capital Ratio down {drop:.2f}pp QoQ, still above "
                f"well-capitalized line ({rbc_old:.2f}% -> {rbc_new:.2f}%)",
                ratio_changes=[c for c in [change_for("total_rbc_ratio")] if c],
            ))

    nim_old, nim_new = prior.get("net_interest_margin"), current.get("net_interest_margin")
    if nim_old is not None and nim_new is not None and change_for("net_interest_margin") and nim_new < nim_old:
        triggers.append(Trigger(
            TIER2, "NIM_COMPRESSION",
            f"Net Interest Margin compressed QoQ ({nim_old:.2f}% -> {nim_new:.2f}%)",
            ratio_changes=[change_for("net_interest_margin")],
        ))

    full_roaa_history = [*roaa_history, roaa_new]
    recent = full_roaa_history[-ROAA_TIER2_CONSECUTIVE_QUARTERS:]
    if len(recent) == ROAA_TIER2_CONSECUTIVE_QUARTERS and all(
        v is not None and v < ROAA_TIER2_FLOOR for v in recent
    ):
        triggers.append(Trigger(
            TIER2, "ROAA_SUSTAINED_LOW",
            f"ROAA below {ROAA_TIER2_FLOOR:.2f}% for {ROAA_TIER2_CONSECUTIVE_QUARTERS}+ "
            f"consecutive quarters (latest {roaa_new:.2f}%)",
            ratio_changes=[c for c in [change_for("roaa")] if c],
        ))

    if prior_total_assets and current_total_assets is not None and is_material_dollar_change(
        prior_total_assets, current_total_assets
    ):
        growth_pct = (current_total_assets - prior_total_assets) / abs(prior_total_assets) * 100
        if growth_pct <= ASSET_GROWTH_TIER2_FLOOR_PCT:
            triggers.append(Trigger(
                TIER2, "ASSET_SHRINKAGE",
                f"Total assets growth rate {growth_pct:.1f}% (below "
                f"{ASSET_GROWTH_TIER2_FLOOR_PCT:g}% annualized floor)",
            ))

    ltd_old, ltd_new = prior.get("net_loans_leases_to_deposits"), current.get("net_loans_leases_to_deposits")
    if ltd_old is not None and ltd_new is not None and (ltd_new - ltd_old) >= LOANS_DEPOSITS_SHARP_INCREASE_PP:
        triggers.append(Trigger(
            TIER2, "LOANS_DEPOSITS_SHARP_UP",
            f"Loans/Deposits ratio up sharply QoQ ({ltd_old:.2f}% -> {ltd_new:.2f}%)",
            ratio_changes=[c for c in [change_for("net_loans_leases_to_deposits")] if c],
        ))

    return triggers


def classify_restatement(
    stored: dict[str, Optional[float]], refiled: dict[str, Optional[float]]
) -> list[Trigger]:
    """A refiling of an already-seen period. Spec §6 scopes this to exactly one
    rule: a capital ratio restated down >50bp is Tier 1 -- everything else about
    a restatement (below the floor, or a non-capital-ratio revision) is Tier 3."""
    triggers = []
    for key in CAPITAL_RATIO_KEYS:
        old_v, new_v = stored.get(key), refiled.get(key)
        if old_v is None or new_v is None:
            continue
        if old_v - new_v > RESTATEMENT_THRESHOLD_PP:
            triggers.append(Trigger(
                TIER1, "CAPITAL_RATIO_RESTATED_DOWN",
                f"Refiling restated {key.replace('_', ' ')} down "
                f"{(old_v - new_v):.2f}pp on a previously-reported period "
                f"({old_v:.2f}% -> {new_v:.2f}%)",
                ratio_changes=diff_ratios({key: old_v}, {key: new_v}),
            ))
    return triggers


_CAPITAL_RATIO_LABELS = {
    "tier1_leverage_ratio": "Tier 1 Leverage Ratio",
    "tier1_rbc_ratio": "Tier 1 Risk-Based Capital Ratio",
    "total_rbc_ratio": "Total Risk-Based Capital Ratio",
    "cet1_ratio": "CET1 Ratio",
}


def describe_restatement_change(
    stored: dict[str, Optional[float]], refiled: dict[str, Optional[float]]
) -> str:
    """One-line "why" for a sub-floor restatement (classify_restatement found no
    capital ratio down >50bp) -- names whichever capital ratio moved most, even
    below that threshold, rather than leaving the reader with just "amended."
    """
    best_key, best_delta = None, 0.0
    for key in CAPITAL_RATIO_KEYS:
        old_v, new_v = stored.get(key), refiled.get(key)
        if old_v is None or new_v is None:
            continue
        delta = new_v - old_v
        if best_key is None or abs(delta) > abs(best_delta):
            best_key, best_delta = key, delta

    if best_key is None:
        return "no capital ratio field present in both versions to compare"
    if best_delta == 0:
        return "capital ratios unchanged -- likely a non-capital field correction"

    old_v, new_v = stored[best_key], refiled[best_key]
    label = _CAPITAL_RATIO_LABELS[best_key]
    return f"{label} {old_v:.2f}% -> {new_v:.2f}% ({best_delta:+.2f}pp)"


def escalate_for_watch_list(triggers: list[Trigger], on_watch_list: bool) -> list[Trigger]:
    """Spec §6 escalation rule: any Tier 2 trigger on a bank already on the Watch
    List auto-escalates to Tier 1."""
    if not on_watch_list:
        return triggers
    escalated = []
    for t in triggers:
        if t.tier == TIER2:
            escalated.append(Trigger(TIER1, t.trigger_type, t.description, t.ratio_changes, escalated=True))
        else:
            escalated.append(t)
    return escalated


def _fmt(value: Optional[float]) -> str:
    return f"{value:.2f}%" if value is not None else "n/a"
