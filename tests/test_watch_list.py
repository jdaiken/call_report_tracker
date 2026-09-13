from datetime import date

from ffiec_monitor import db
from ffiec_monitor.watch_list import (
    determine_reason,
    has_qualifying_order,
    is_ltwc,
    recompute_membership,
)


def test_is_ltwc_true_when_any_pca_threshold_breached():
    assert is_ltwc({"tier1_leverage_ratio": 4.9, "total_rbc_ratio": 12, "tier1_rbc_ratio": 9, "cet1_ratio": 8})
    assert is_ltwc({"tier1_leverage_ratio": 6, "total_rbc_ratio": 9.9, "tier1_rbc_ratio": 9, "cet1_ratio": 8})
    assert is_ltwc({"tier1_leverage_ratio": 6, "total_rbc_ratio": 12, "tier1_rbc_ratio": 7.9, "cet1_ratio": 8})
    assert is_ltwc({"tier1_leverage_ratio": 6, "total_rbc_ratio": 12, "tier1_rbc_ratio": 9, "cet1_ratio": 6.4})


def test_is_ltwc_false_when_well_capitalized():
    assert not is_ltwc({"tier1_leverage_ratio": 6, "total_rbc_ratio": 12, "tier1_rbc_ratio": 9, "cet1_ratio": 8})


def test_cmp_alone_does_not_qualify():
    assert not has_qualifying_order(["CMP"])


def test_safety_and_soundness_or_aml_qualifies():
    assert has_qualifying_order(["SAFETY_SOUNDNESS"])
    assert has_qualifying_order(["AML_BSA"])


def test_cmp_plus_safety_and_soundness_still_qualifies():
    assert has_qualifying_order(["CMP", "SAFETY_SOUNDNESS"])


def test_determine_reason_both():
    assert determine_reason(True, True) == "BOTH"
    assert determine_reason(True, False) == "LTWC"
    assert determine_reason(False, True) == "ORDER"
    assert determine_reason(False, False) is None


def _well_capitalized_ratios():
    return {"tier1_leverage_ratio": 8, "total_rbc_ratio": 14, "tier1_rbc_ratio": 12, "cet1_ratio": 11}


def _ltwc_ratios():
    return {"tier1_leverage_ratio": 3.5, "total_rbc_ratio": 14, "tier1_rbc_ratio": 12, "cet1_ratio": 11}


def test_recompute_membership_add_and_remove_roundtrip():
    conn = db.connect(":memory:")
    change = recompute_membership(conn, "1", _ltwc_ratios(), [], date(2024, 1, 1))
    assert change is not None and change.change_type == "ADDED" and change.reason == "LTWC"

    # still LTWC next run -- no change event
    change = recompute_membership(conn, "1", _ltwc_ratios(), [], date(2024, 4, 1))
    assert change is None

    # recovers -- should be removed
    change = recompute_membership(conn, "1", _well_capitalized_ratios(), [], date(2024, 7, 1))
    assert change is not None and change.change_type == "REMOVED"

    entry = db.get_watch_list_entry(conn, "1")
    assert entry is None


def test_recompute_membership_reason_change_from_ltwc_to_both():
    conn = db.connect(":memory:")
    recompute_membership(conn, "2", _ltwc_ratios(), [], date(2024, 1, 1))
    change = recompute_membership(conn, "2", _ltwc_ratios(), ["SAFETY_SOUNDNESS"], date(2024, 4, 1))
    assert change is not None
    assert change.change_type == "REASON_CHANGED"
    assert change.reason == "BOTH"
    assert change.previous_reason == "LTWC"
