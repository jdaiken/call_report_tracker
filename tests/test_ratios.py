from ffiec_monitor.ratios import diff_ratios, full_snapshot_rows, is_material_dollar_change


def test_diff_ratios_filters_sub_floor_changes():
    old = {"tier1_leverage_ratio": 8.00, "roaa": 1.00}
    new = {"tier1_leverage_ratio": 8.04, "roaa": 1.00}  # 4bp move, below the 5bp floor
    changes = diff_ratios(old, new)
    assert changes == []


def test_diff_ratios_keeps_at_floor_changes():
    old = {"tier1_leverage_ratio": 8.00}
    new = {"tier1_leverage_ratio": 8.05}  # exactly 5bp
    changes = diff_ratios(old, new)
    assert len(changes) == 1
    assert changes[0].key == "tier1_leverage_ratio"
    assert changes[0].old_value == 8.00
    assert changes[0].new_value == 8.05


def test_diff_ratios_charge_off_carries_peer_average_without_its_own_old_new():
    old = {"charge_off_ratio": 0.20}
    new = {"charge_off_ratio": 0.80, "peer_avg_charge_off_ratio": 0.35}
    changes = diff_ratios(old, new)
    assert len(changes) == 1
    c = changes[0]
    assert c.key == "charge_off_ratio"
    assert c.old_value == 0.20 and c.new_value == 0.80
    assert c.peer_value == 0.35
    assert c.paired is None


def test_diff_ratios_loan_growth_and_capital_growth_are_two_separate_rows():
    old = {"loan_growth_rate": 2.0, "capital_growth_rate": 1.0}
    new = {"loan_growth_rate": 9.0, "capital_growth_rate": 1.02}  # capital growth barely moves
    changes = diff_ratios(old, new)
    assert len(changes) == 1
    loan_change = changes[0]
    assert loan_change.key == "loan_growth_rate"
    assert loan_change.old_value == 2.0 and loan_change.new_value == 9.0
    # capital growth moved only 2bp -- below the floor -- so it should NOT be paired in
    assert loan_change.paired is None


def test_diff_ratios_pairs_capital_growth_when_it_also_moves_materially():
    old = {"loan_growth_rate": 2.0, "capital_growth_rate": 1.0}
    new = {"loan_growth_rate": 9.0, "capital_growth_rate": -3.0}
    changes = diff_ratios(old, new)
    assert len(changes) == 1
    loan_change = changes[0]
    assert loan_change.paired is not None
    assert loan_change.paired.key == "capital_growth_rate"
    assert loan_change.paired.old_value == 1.0
    assert loan_change.paired.new_value == -3.0


def test_diff_ratios_new_ratio_appearing_is_always_material():
    old = {"roaa": None}
    new = {"roaa": 1.00}
    changes = diff_ratios(old, new)
    assert len(changes) == 1


def test_full_snapshot_rows_returns_all_twelve_ratios():
    ratios = {"tier1_leverage_ratio": 5.0}
    rows = full_snapshot_rows(ratios)
    assert len(rows) == 12
    assert all(r.old_value is None for r in rows)


def test_is_material_dollar_change_relative_floor():
    assert not is_material_dollar_change(1_000_000, 1_005_000)  # 0.5% -- below 1% floor
    assert is_material_dollar_change(1_000_000, 1_010_000)      # exactly 1%
