"""extract_ratios / compute_growth_rates against a real Call Report facsimile.

The fixture below is RSSD 37 (Bank of Hancock County, GA), period 2026-06-30,
values taken verbatim from a real FFIEC CDR bulk Call Report download -- not
synthesized. It's a Community Bank Leverage Ratio (CBLR) filer, which is why the
RBC/CET1 ratios are genuinely blank rather than a parsing gap.
"""
from datetime import date

import pytest

from ffiec_monitor.ratios import annualization_factor, compute_growth_rates, extract_ratios

HANCOCK_FACSIMILE = {
    "concepts": {
        "RCON2170": "85210",       # total assets
        "RCONB529": "16787",       # net loans & leases
        "RCON2200": "71354",       # total deposits (domestic)
        "RCON1773": "50465",       # AFS securities
        "RCONJJ34": "0",           # HTM securities
        "RCON3190": "0",           # other borrowed money
        "RCONB995": "0",           # securities sold under repo
        "RCONB989": "0",           # securities purchased under resell
        "RCON0071": "13644",       # interest-bearing balances due from banks
        "RCON3210": "13654",       # total equity capital
        "RCOA7204": "20.1508%",    # Tier 1 leverage ratio
        "RCON0416": "23243",       # pledged securities
        "RCON2365": "0",           # brokered deposits
        "RCONJ474": "2578",        # time deposits > $250k
        "RCONJH83": "0",           # reciprocal deposits
        "RCON1403": "48",          # nonaccrual
        "RCON1407": "96",          # 90+ days past due
        "RCON3360": "17215",       # avg total loans
        "RCONB558": "7980",        # avg securities: treasury
        "RCONB559": "7152",        # avg securities: MBS
        "RCONB560": "39741",       # avg securities: other
        "RCON3365": "0",           # avg fed funds sold/resell
        "RCON3381": "14788",       # avg due-from-banks
        "RCON3368": "86194",       # avg total assets
        "RIAD4340": "196",         # YTD net income
        "RIAD4074": "1384",        # YTD net interest income
        "RIAD4638": "0",           # charge-offs: C&I
        "RIADB514": "0",           # charge-offs: credit card
        "RIADC891": "0",           # charge-offs: 1-4 fam construction
        "RIADK205": "1",           # charge-offs: all other
        "RIAD4608": "0",           # recoveries: C&I
        "RIADB515": "0",           # recoveries: credit card
        "RIADC892": "0",           # recoveries: 1-4 fam construction
        "RIADK206": "0",           # recoveries: all other
        # RCON1773's fields with no RCFD counterpart present -> domestic-only filer,
        # matching what we verified: RCFD-prefixed codes are blank for ~98% of banks.
    }
}

PERIOD_END = date(2026, 6, 30)


def test_annualization_factor():
    assert annualization_factor(date(2026, 3, 31)) == pytest.approx(4.0)
    assert annualization_factor(date(2026, 6, 30)) == pytest.approx(2.0)
    assert annualization_factor(date(2026, 9, 30)) == pytest.approx(4 / 3)
    assert annualization_factor(date(2026, 12, 31)) == pytest.approx(1.0)


def test_extract_ratios_cblr_bank_leverage_only():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["tier1_leverage_ratio"] == pytest.approx(20.1508)
    # CBLR filer -- these are genuinely blank on the real facsimile, not a bug.
    assert out["tier1_rbc_ratio"] is None
    assert out["total_rbc_ratio"] is None
    assert out["cet1_ratio"] is None


def test_extract_ratios_loans_to_deposits():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["net_loans_leases_to_deposits"] == pytest.approx(23.52636, rel=1e-4)


def test_extract_ratios_pledged_securities():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["pledged_securities_ratio"] == pytest.approx(46.05766, rel=1e-4)


def test_extract_ratios_deposit_composition():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["brokered_reciprocal_deposits_to_total_deposits"] == pytest.approx(0.0)
    assert out["core_deposits_to_total_deposits"] == pytest.approx(96.38703, rel=1e-4)


def test_extract_ratios_noncurrent_oreo():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["noncurrent_loans_oreo_ratio"] == pytest.approx(0.85781, rel=1e-4)


def test_extract_ratios_roaa_and_nim_annualized():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["roaa"] == pytest.approx(0.454788, rel=1e-4)
    assert out["net_interest_margin"] == pytest.approx(3.18615, rel=1e-4)


def test_extract_ratios_charge_off():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["charge_off_ratio"] == pytest.approx(0.0116178, rel=1e-3)
    assert out["peer_avg_charge_off_ratio"] is None  # not computed -- see module docstring


def test_extract_ratios_growth_rates_not_set_without_prior_period():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["loan_growth_rate"] is None
    assert out["capital_growth_rate"] is None


def test_extract_ratios_stashes_raw_balances_for_growth():
    out = extract_ratios(HANCOCK_FACSIMILE, PERIOD_END)
    assert out["_net_loans_raw"] == pytest.approx(16787)
    assert out["_total_equity_raw"] == pytest.approx(13654)


def test_compute_growth_rates_annualizes_over_actual_months_elapsed():
    prior = {"_net_loans_raw": 100.0, "_total_equity_raw": 50.0}
    current = {"_net_loans_raw": 103.0, "_total_equity_raw": 49.0}
    compute_growth_rates(prior, current, date(2026, 3, 31), date(2026, 6, 30))
    # 3% growth over 3 months, annualized (x4) -> 12%
    assert current["loan_growth_rate"] == pytest.approx(12.0)
    # -2% over 3 months, annualized -> -8%
    assert current["capital_growth_rate"] == pytest.approx(-8.0)


def test_compute_growth_rates_noop_without_prior_data():
    current = {"_net_loans_raw": 103.0}
    compute_growth_rates({}, current, date(2026, 3, 31), date(2026, 6, 30))
    assert "loan_growth_rate" not in current or current.get("loan_growth_rate") is None
