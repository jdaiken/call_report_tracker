#!/usr/bin/env python3
"""Historical validation using SVB as a test case -- spec §10.3.

Walks Silicon Valley Bank's known pre-failure quarters (2021 Q1 through 2022 Q4 --
SVB failed mid-Q1 2023, so there is no Q1 2023 Call Report to expect) through the
same ratio/alert logic the daily pipeline uses, and prints a quarter-by-quarter
timeline of what fired.

IMPORTANT -- entity scoping (spec §10.3, and the same distinction that came up with
Banc of California): this must run against Silicon Valley Bank the *bank subsidiary*'s
RSSD ID, NOT SVB Financial Group, the holding company. Call Reports are filed at the
bank-subsidiary level; the holding company files different reports (FR Y-9C) with a
different RSSD and won't have Call Report/UBPR data at all.

SVB_BANK_RSSD below is filled in from public record but has NOT been re-verified
against a live FFIEC_TOKEN in this build environment -- confirm it via
`RetrievePanelOfReporters` (or the FFIEC NIC institution search) before trusting
backtest output, and confirm the resolved institution name actually reads
"Silicon Valley Bank", not "SVB Financial Group", before proceeding.

Requires FFIEC_USER_ID / FFIEC_TOKEN to actually execute (this cannot be run inside
this build/review environment, which has no live token).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import alerts, ratios
from ffiec_monitor.config import FFIECConfig
from ffiec_monitor.ffiec_client import FFIECClient

# Silicon Valley Bank, the bank subsidiary -- VERIFY before relying on this.
SVB_BANK_RSSD = "802866"

# Pre-failure quarters only; SVB failed mid-Q1 2023 so no Q1 2023 Call Report exists.
BACKTEST_QUARTERS = [
    date(2021, 3, 31), date(2021, 6, 30), date(2021, 9, 30), date(2021, 12, 31),
    date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31),
]


def _verify_entity(client: FFIECClient, period_end: date) -> None:
    """RetrieveFacsimile carries no institution name -- that only exists on the
    Panel of Reporters. Check it once, against the first backtest quarter, rather
    than skip the entity-scoping safety check spec §10.3 explicitly calls for."""
    panel = {str(e["ID_RSSD"]): e for e in client.retrieve_panel_of_reporters(period_end)}
    entry = panel.get(SVB_BANK_RSSD)
    if entry is None:
        print(f"  WARNING: RSSD {SVB_BANK_RSSD} not found on the {period_end} panel of reporters at all -- "
              f"double check SVB_BANK_RSSD before trusting any of this.")
        return
    name = str(entry.get("Name", ""))
    if "silicon valley bank" not in name.lower():
        print(f"  WARNING: RSSD {SVB_BANK_RSSD} resolves to {name!r}, not 'Silicon Valley Bank' -- "
              f"check SVB_BANK_RSSD before trusting this run (this is exactly the bank-subsidiary-"
              f"vs-holding-company mixup spec §10.3 warns about).")
    else:
        print(f"  Entity check OK: RSSD {SVB_BANK_RSSD} = {name!r}\n")


def run_backtest(client: FFIECClient) -> None:
    prior_ratios: dict = {}
    prior_period_end: date = None
    roaa_history: list = []
    prior_assets = None

    print(f"SVB backtest -- RSSD {SVB_BANK_RSSD} -- {BACKTEST_QUARTERS[0]} through {BACKTEST_QUARTERS[-1]}\n")
    _verify_entity(client, BACKTEST_QUARTERS[0])

    for period_end in BACKTEST_QUARTERS:
        facsimile = client.retrieve_facsimile(SVB_BANK_RSSD, period_end)

        current_ratios = ratios.extract_ratios(facsimile, period_end)
        current_assets = ratios.extract_total_assets(facsimile)
        current_ratios["total_assets"] = current_assets

        if prior_ratios:
            ratios.compute_growth_rates(prior_ratios, current_ratios, prior_period_end, period_end)

        triggers = alerts.classify_new_period(
            prior_ratios, current_ratios, roaa_history, prior_assets, current_assets,
        )

        print(f"=== {period_end.isoformat()} ===")
        print(f"  Tier1 Leverage: {current_ratios.get('tier1_leverage_ratio')}  "
              f"Total RBC: {current_ratios.get('total_rbc_ratio')}  "
              f"ROAA: {current_ratios.get('roaa')}")
        if not triggers:
            print("  (no triggers)")
        for t in triggers:
            print(f"  [{t.tier}] {t.trigger_type}: {t.description}")
        print()

        roaa_history = (roaa_history + [current_ratios.get("roaa")])[-alerts.ROAA_TIER2_CONSECUTIVE_QUARTERS:]
        prior_ratios = current_ratios
        prior_period_end = period_end
        prior_assets = current_assets

    print(
        "VALIDATION CHECK: confirm above that capital pressure (leverage/RBC ratio\n"
        "decline or PCA-boundary crossings) and any available AFS/HTM securities\n"
        "deterioration signal shows up as Tier 1/Tier 2 triggers in the quarters\n"
        "leading up to Q4 2022. Note: unrealized AFS/HTM securities losses are NOT\n"
        "one of the 12 ratios of concern (spec §5) and are not extracted by\n"
        "ffiec_monitor.ratios today -- if this backtest doesn't surface that specific\n"
        "stress on its own, that is a real gap to fold back into the ratio set,\n"
        "not a bug in this script."
    )


if __name__ == "__main__":
    run_backtest(FFIECClient(FFIECConfig.from_env()))
