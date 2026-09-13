"""Watch List membership -- spec §4. Recomputed fresh every daily run; a bank
qualifies if EITHER the LTWC capital test or the enforcement-order test fires.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import duckdb

from . import db
from .ratios import CET1_KEY

# PCA well-capitalized thresholds (spec §4). Below ANY one -> Less Than Well Capitalized.
LTWC_THRESHOLDS = {
    "tier1_leverage_ratio": 5.0,
    "total_rbc_ratio": 10.0,
    "tier1_rbc_ratio": 8.0,
    CET1_KEY: 6.5,
}

# CMP alone never qualifies; Safety & Soundness or AML/BSA does (spec §4).
QUALIFYING_ORDER_TYPES = {"SAFETY_SOUNDNESS", "AML_BSA"}


def is_ltwc(ratios: dict[str, Optional[float]]) -> bool:
    for key, threshold in LTWC_THRESHOLDS.items():
        value = ratios.get(key)
        if value is not None and value < threshold:
            return True
    return False


def has_qualifying_order(active_order_types: list[str]) -> bool:
    return any(t in QUALIFYING_ORDER_TYPES for t in active_order_types)


def determine_reason(ltwc: bool, has_order: bool) -> Optional[str]:
    if ltwc and has_order:
        return "BOTH"
    if ltwc:
        return "LTWC"
    if has_order:
        return "ORDER"
    return None


@dataclass(frozen=True)
class WatchListChange:
    id_rssd: str
    change_type: str  # 'ADDED' | 'REMOVED' | 'REASON_CHANGED'
    reason: Optional[str]
    previous_reason: Optional[str] = None


def recompute_membership(
    conn: duckdb.DuckDBPyConnection,
    id_rssd: str,
    ratios: dict[str, Optional[float]],
    active_order_types: list[str],
    as_of: date,
) -> Optional[WatchListChange]:
    """Re-evaluate one bank's Watch List status and persist the result.

    Returns a WatchListChange when membership was added, removed, or the
    qualifying reason changed while remaining a member -- these are exactly the
    events called out at the top of the email (spec §7.1). Returns None for a
    bank whose status is unchanged (still off the list, or still on it for the
    same reason).
    """
    qualifies = determine_reason(is_ltwc(ratios), has_qualifying_order(active_order_types))
    existing = db.get_watch_list_entry(conn, id_rssd)

    if qualifies and existing is None:
        db.add_to_watch_list(conn, id_rssd, qualifies, as_of)
        return WatchListChange(id_rssd, "ADDED", qualifies)

    if qualifies and existing is not None:
        if existing.reason != qualifies:
            db.update_watch_list_reason(conn, id_rssd, qualifies)
            return WatchListChange(id_rssd, "REASON_CHANGED", qualifies, existing.reason)
        return None

    if not qualifies and existing is not None:
        db.remove_from_watch_list(conn, id_rssd, as_of)
        return WatchListChange(id_rssd, "REMOVED", None, existing.reason)

    return None
