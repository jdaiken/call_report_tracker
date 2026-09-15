"""Loads and resolves config/ubpr_field_mapping.csv (spec §10).

The CSV is the real artifact -- checked once against the UBPR User's Guide, not
inline renaming logic buried in a script (spec §10). This module only loads and
resolves it; it never invents field IDs itself.

**Ships with every raw_field_id set to "TBD" and verified=FALSE.** Confirming the
actual FFIEC field codes requires the UBPR User's Guide at cdr.ffiec.gov, which
this build environment has no network access to (see historical-risk-spec.md's
top-level caveat in README.md) -- filling those in, against the guide version
that matches the years being pulled (spec §10: "v175, v181... confirm which
version's field list matches which years"), is the first real step before
running pull.py for real. `validate()` below fails loudly rather than silently
pulling garbage if that step is skipped.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent.parent / "config" / "ubpr_field_mapping.csv"

# The ratio logical_names ubpr_ratios (historical_risk/db.py) has columns for --
# shared by cleanup.py (which fails loudly if the mapping config is missing any
# of these) and field_stats.py (which aggregates over exactly this set). Does
# NOT include the four identifier rows (id_rssd/period_end/bank_name/
# peer_group_id) or camels_composite, which isn't sourced from UBPR at all.
EXPECTED_RATIO_LOGICAL_NAMES = [
    "tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio", "cet1_ratio",
    "roaa", "net_interest_margin", "noncurrent_loans_oreo_ratio", "efficiency_ratio",
    "net_loans_leases_to_deposits", "net_noncore_funding_dependence",
    "charge_off_ratio", "loan_growth_rate", "capital_growth_rate",
]


@dataclass(frozen=True)
class FieldMapping:
    logical_name: str
    raw_field_id: str
    ubpr_page: str
    active_from: Optional[date]
    active_to: Optional[date]
    verified: bool
    notes: str

    def active_on(self, period_end: date) -> bool:
        if self.active_from is not None and period_end < self.active_from:
            return False
        if self.active_to is not None and period_end > self.active_to:
            return False
        return True


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_bool(s: str) -> bool:
    return (s or "").strip().upper() == "TRUE"


def load_mapping(path: Path = DEFAULT_MAPPING_PATH) -> list[FieldMapping]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        return [
            FieldMapping(
                logical_name=row["logical_name"].strip(),
                raw_field_id=row["raw_field_id"].strip(),
                ubpr_page=row["ubpr_page"].strip(),
                active_from=_parse_date(row["active_from"]),
                active_to=_parse_date(row["active_to"]),
                verified=_parse_bool(row["verified"]),
                notes=row.get("notes", "").strip(),
            )
            for row in reader
        ]


class UnverifiedMappingError(RuntimeError):
    """Raised when the field mapping hasn't been checked against the real UBPR
    User's Guide yet -- see module docstring. Not something to catch and ignore;
    fix config/ubpr_field_mapping.csv instead."""


def validate(mappings: list[FieldMapping], require_verified: bool = True) -> None:
    unverified = [m.logical_name for m in mappings if not m.verified or m.raw_field_id == "TBD"]
    if unverified and require_verified:
        raise UnverifiedMappingError(
            "The following logical_name entries in config/ubpr_field_mapping.csv still "
            "have raw_field_id='TBD' / verified=FALSE: "
            f"{', '.join(unverified)}. Confirm each against the FFIEC UBPR User's Guide "
            "(cdr.ffiec.gov -- pick the guide version matching the years you're pulling, "
            "spec §10) and set both columns before running cleanup.py for real. Pass "
            "require_verified=False only for a dry run against synthetic/test data."
        )

    seen: dict[str, FieldMapping] = {}
    for m in mappings:
        if m.logical_name in seen:
            raise ValueError(f"Duplicate logical_name in mapping config: {m.logical_name!r}")
        seen[m.logical_name] = m


def active_mapping_for_period(mappings: list[FieldMapping], period_end: date) -> dict[str, FieldMapping]:
    """logical_name -> FieldMapping for whichever mappings are active on period_end
    (spec §11's active_from/active_to mechanism). A logical_name simply absent from
    the result (e.g. cet1_ratio before 2015-01-01) should be left NULL downstream,
    never zero-filled -- spec §11's explicit instruction."""
    return {m.logical_name: m for m in mappings if m.active_on(period_end)}


def raw_field_ids_needed(mappings: list[FieldMapping]) -> set[str]:
    return {m.raw_field_id for m in mappings}
