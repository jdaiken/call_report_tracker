from ffiec_monitor import alerts


def test_leverage_crossing_5pct_is_tier1():
    prior = {"tier1_leverage_ratio": 5.2}
    current = {"tier1_leverage_ratio": 4.8}
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    tier1_types = {t.trigger_type for t in triggers if t.tier == alerts.TIER1}
    assert "LEVERAGE_PCA_CROSSING" in tier1_types


def test_leverage_crossing_multiple_boundaries_in_one_move():
    prior = {"tier1_leverage_ratio": 5.5}
    current = {"tier1_leverage_ratio": 2.5}  # crosses 5%, 4%, and 3% all at once
    triggers = [t for t in alerts.classify_new_period(prior, current, [], None, None)
                if t.trigger_type == "LEVERAGE_PCA_CROSSING"]
    assert len(triggers) == 3


def test_roaa_goes_negative_is_tier1_but_not_every_subsequent_negative_quarter():
    triggers = alerts.classify_new_period({"roaa": 0.10}, {"roaa": -0.05}, [], None, None)
    assert any(t.trigger_type == "ROAA_NEGATIVE" for t in triggers)

    # still negative next quarter -- shouldn't re-fire the "goes negative" trigger
    triggers = alerts.classify_new_period({"roaa": -0.05}, {"roaa": -0.30}, [], None, None)
    assert not any(t.trigger_type == "ROAA_NEGATIVE" for t in triggers)


def test_noncurrent_oreo_doubling_is_tier1():
    prior = {"noncurrent_loans_oreo_ratio": 1.0}
    current = {"noncurrent_loans_oreo_ratio": 2.1}
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    assert any(t.trigger_type == "NONCURRENT_OREO_STRESS" for t in triggers)


def test_noncurrent_oreo_crossing_absolute_threshold_is_tier1():
    prior = {"noncurrent_loans_oreo_ratio": 2.5}
    current = {"noncurrent_loans_oreo_ratio": 3.1}  # +24% only, but crosses 3%
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    assert any(t.trigger_type == "NONCURRENT_OREO_STRESS" for t in triggers)


def test_leverage_down_100bp_while_still_well_capitalized_is_tier2():
    prior = {"tier1_leverage_ratio": 9.0}
    current = {"tier1_leverage_ratio": 7.8}  # -120bp, still well above 5%
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    tier2_types = {t.trigger_type for t in triggers if t.tier == alerts.TIER2}
    assert "LEVERAGE_DOWN_100BP" in tier2_types


def test_large_drop_that_crosses_pca_boundary_is_not_double_counted_as_tier2():
    prior = {"tier1_leverage_ratio": 6.5}
    current = {"tier1_leverage_ratio": 4.5}  # -200bp AND crosses 5% -- should be Tier1 only
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    assert not any(t.trigger_type == "LEVERAGE_DOWN_100BP" for t in triggers)
    assert any(t.trigger_type == "LEVERAGE_PCA_CROSSING" for t in triggers)


def test_roaa_sustained_low_two_quarters_is_tier2():
    # two prior quarters both below 0.50%, current also below -> fires
    triggers = alerts.classify_new_period(
        {"roaa": 0.30}, {"roaa": 0.20}, roaa_history=[0.40], prior_total_assets=None, current_total_assets=None,
    )
    assert any(t.trigger_type == "ROAA_SUSTAINED_LOW" for t in triggers)


def test_asset_shrinkage_below_floor_is_tier2():
    triggers = alerts.classify_new_period({}, {}, [], prior_total_assets=1_000_000, current_total_assets=880_000)
    assert any(t.trigger_type == "ASSET_SHRINKAGE" for t in triggers)


def test_restatement_capital_ratio_down_over_50bp_is_tier1():
    stored = {"tier1_leverage_ratio": 8.00}
    refiled = {"tier1_leverage_ratio": 7.40}  # -60bp
    triggers = alerts.classify_restatement(stored, refiled)
    assert len(triggers) == 1
    assert triggers[0].tier == alerts.TIER1
    assert triggers[0].trigger_type == "CAPITAL_RATIO_RESTATED_DOWN"


def test_restatement_capital_ratio_down_under_50bp_is_not_tier1():
    stored = {"tier1_leverage_ratio": 8.00}
    refiled = {"tier1_leverage_ratio": 7.60}  # -40bp
    triggers = alerts.classify_restatement(stored, refiled)
    assert triggers == []


def test_restatement_capital_ratio_restated_up_never_triggers():
    stored = {"tier1_leverage_ratio": 7.00}
    refiled = {"tier1_leverage_ratio": 8.00}
    triggers = alerts.classify_restatement(stored, refiled)
    assert triggers == []


def test_escalation_rule_promotes_tier2_to_tier1_for_watch_list_banks():
    prior = {"tier1_leverage_ratio": 9.0}
    current = {"tier1_leverage_ratio": 7.8}  # Tier 2 on its own
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    assert all(t.tier == alerts.TIER2 for t in triggers)

    escalated = alerts.escalate_for_watch_list(triggers, on_watch_list=True)
    assert all(t.tier == alerts.TIER1 for t in escalated)
    assert all(t.escalated for t in escalated)


def test_escalation_rule_no_op_when_not_on_watch_list():
    prior = {"tier1_leverage_ratio": 9.0}
    current = {"tier1_leverage_ratio": 7.8}
    triggers = alerts.classify_new_period(prior, current, [], None, None)
    escalated = alerts.escalate_for_watch_list(triggers, on_watch_list=False)
    assert escalated == triggers
