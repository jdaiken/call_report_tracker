"""Level screen for new quarterly filings: flags anything concerning in a bank's
latest ratios even when no Tier 1/2 threshold (which are change-based) fired.

The lens is a deposit-network / funding counterparty: can this bank keep taking
and holding reciprocal or brokered deposits, and is it showing strain? Capital
matters most because a bank that falls below well capitalized faces limits on
brokered deposits and on how much reciprocal funding it can exclude from them.

Ranges are rules of thumb (easy to change below), not regulatory limits.
`concern` = look at this filing now; `watch` = note it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Flag:
    level: str          # "concern" | "watch"
    key: str
    text: str           # plain English, with the number


def _v(r: dict, k: str) -> Optional[float]:
    v = r.get(k)
    return float(v) if isinstance(v, (int, float)) else None


def screen(r: Optional[dict]) -> list[Flag]:
    if not r:
        return []
    out: list[Flag] = []

    def add(level, key, text):
        out.append(Flag(level, key, text))

    lev = _v(r, "tier1_leverage_ratio")
    if lev is not None:
        if lev < 6:
            add("concern", "lev", f"Leverage ratio {lev:.2f}%: near the 5% well-capitalized line")
        elif lev < 8:
            add("watch", "lev", f"Leverage ratio {lev:.2f}%: thin capital cushion")
    rbc = _v(r, "total_rbc_ratio")
    if rbc is not None:
        if rbc < 11:
            add("concern", "rbc", f"Total risk-based capital {rbc:.2f}%: near the 10% well-capitalized line")
        elif rbc < 12.5:
            add("watch", "rbc", f"Total risk-based capital {rbc:.2f}%: thin capital cushion")
    roaa = _v(r, "roaa")
    if roaa is not None:
        if roaa < 0:
            add("concern", "roaa", f"Losing money: return on assets {roaa:.2f}%")
        elif roaa < 0.4:
            add("watch", "roaa", f"Weak earnings: return on assets {roaa:.2f}%")
    nim = _v(r, "net_interest_margin")
    if nim is not None and nim < 2.5:
        add("watch", "nim", f"Net interest margin {nim:.2f}%: squeezed by funding costs")
    ltd = _v(r, "net_loans_leases_to_deposits")
    if ltd is not None:
        if ltd > 110:
            add("concern", "ltd", f"Loans are {ltd:.0f}% of deposits: relies on borrowed money")
        elif ltd > 100:
            add("watch", "ltd", f"Loans are {ltd:.0f}% of deposits")
    nncf = _v(r, "net_noncore_funding_dependence_ratio")
    if nncf is not None:
        if nncf > 35:
            add("concern", "nncf", f"Non-core funding dependence {nncf:.1f}%: heavy reliance on rate-sensitive money")
        elif nncf > 25:
            add("watch", "nncf", f"Non-core funding dependence {nncf:.1f}%")
    pledged = _v(r, "pledged_securities_ratio")
    if pledged is not None and pledged > 75:
        add("watch", "pledged", f"{pledged:.0f}% of securities pledged: little left to raise cash")
    core = _v(r, "core_deposits_to_total_deposits")
    if core is not None:
        if core < 60:
            add("concern", "core", f"Core deposits only {core:.0f}% of deposits")
        elif core < 70:
            add("watch", "core", f"Core deposits {core:.0f}% of deposits")
    br = _v(r, "brokered_reciprocal_deposits_to_total_deposits")
    if br is not None:
        if br > 25:
            add("concern", "br", f"Brokered/reciprocal deposits {br:.1f}% of deposits: heavy network reliance")
        elif br > 15:
            add("watch", "br", f"Brokered/reciprocal deposits {br:.1f}% of deposits")
    co, peer = _v(r, "charge_off_ratio"), _v(r, "peer_avg_charge_off_ratio")
    if co is not None and peer is not None and peer > 0 and co > 2 * peer and co >= 0.25:
        add("watch", "co", f"Charge-offs {co:.2f}% vs. peer {peer:.2f}%")

    # A capital flag plus heavy brokered/reciprocal use is the combination that matters most
    # for a deposit network: falling below well capitalized would restrict that funding.
    keys = {f.key for f in out}
    if keys & {"lev", "rbc"} and "br" in keys:
        out = [Flag("concern", f.key, f.text) if f.key == "br" else f for f in out]
        add("concern", "combo", "Thin capital and heavy brokered/reciprocal funding together: a capital slip "
                                "would restrict that funding")
    out.sort(key=lambda f: f.level != "concern")
    return out


def severity(flags: list[Flag]) -> tuple[int, int]:
    return (sum(f.level == "concern" for f in flags), sum(f.level == "watch" for f in flags))
