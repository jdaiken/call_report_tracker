from datetime import date

import pytest

from historical_risk.field_mapping import (
    FieldMapping,
    UnverifiedMappingError,
    active_mapping_for_period,
    load_mapping,
    validate,
)


def test_shipped_mapping_config_loads_and_covers_spec_ratios():
    mappings = load_mapping()
    names = {m.logical_name for m in mappings}
    for expected in [
        "tier1_leverage_ratio", "tier1_rbc_ratio", "total_rbc_ratio", "cet1_ratio",
        "roaa", "net_interest_margin", "efficiency_ratio",
    ]:
        assert expected in names


def test_shipped_mapping_is_unverified_and_validate_raises_by_default():
    # Documents the current state (spec §10's verification step hasn't been done
    # here -- no network access to the UBPR User's Guide, see field_mapping.py).
    mappings = load_mapping()
    assert all(not m.verified for m in mappings)
    with pytest.raises(UnverifiedMappingError):
        validate(mappings)


def test_validate_passes_when_require_verified_false():
    mappings = load_mapping()
    validate(mappings, require_verified=False)  # should not raise


def test_validate_rejects_duplicate_logical_names():
    mappings = [
        FieldMapping("roaa", "X1", "page", None, None, True, ""),
        FieldMapping("roaa", "X2", "page", None, None, True, ""),
    ]
    with pytest.raises(ValueError):
        validate(mappings, require_verified=False)


def test_active_on_respects_active_from_and_to():
    m = FieldMapping("cet1_ratio", "X", "page", date(2015, 1, 1), None, True, "")
    assert not m.active_on(date(2014, 12, 31))
    assert m.active_on(date(2015, 1, 1))
    assert m.active_on(date(2026, 6, 30))


def test_active_on_respects_active_to():
    m = FieldMapping("old_tdr_field", "X", "page", date(2012, 1, 1), date(2024, 3, 31), True, "")
    assert m.active_on(date(2024, 3, 31))
    assert not m.active_on(date(2024, 6, 30))


def test_active_mapping_for_period_excludes_cet1_before_2015():
    mappings = [
        FieldMapping("cet1_ratio", "X", "page", date(2015, 1, 1), None, True, ""),
        FieldMapping("roaa", "Y", "page", date(2012, 1, 1), None, True, ""),
    ]
    active_2013 = active_mapping_for_period(mappings, date(2013, 3, 31))
    assert "cet1_ratio" not in active_2013
    assert "roaa" in active_2013

    active_2016 = active_mapping_for_period(mappings, date(2016, 3, 31))
    assert "cet1_ratio" in active_2016
