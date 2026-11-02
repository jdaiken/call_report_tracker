"""Quarterly risk tests (ffiec_monitor/risk_tests.py, scripts/send_risk_tests.py) on a synthetic database:
150 ordinary banks plus planted cases each test should (or should not) catch."""
import io
import random
from datetime import date

import openpyxl
import pytest

from ffiec_monitor import db, ratios as R, risk_tests as RT

Q3, Q2 = date(2026, 9, 30), date(2026, 6, 30)


def _ordinary(rng, assets):
    return {"total_assets": assets, R.DEPOSITS_RAW_KEY: assets * 0.85, R.UNINSURED_RAW_KEY: assets * 0.2,
            R.RECIPROCAL_RAW_KEY: assets * 0.02,
            "tier1_leverage_ratio": rng.uniform(8, 14), "tier1_rbc_ratio": rng.uniform(11, 18),
            "total_rbc_ratio": rng.uniform(12, 19), "cet1_ratio": rng.uniform(11, 18),
            "roaa": rng.uniform(0.4, 1.6), "net_interest_margin": rng.uniform(2.8, 4.2),
            "efficiency_ratio": rng.uniform(50, 75), "noncurrent_loans_oreo_ratio": rng.uniform(0, 1.2),
            "npa_to_assets": rng.uniform(0, 0.8), "texas_ratio": rng.uniform(0, 15), "charge_off_ratio": rng.uniform(0, 0.4),
            "uninsured_deposit_share": rng.uniform(15, 35), "liquid_to_uninsured": rng.uniform(110, 300),
            "brokered_reciprocal_deposits_to_total_deposits": rng.uniform(0, 10),
            "net_loans_leases_to_deposits": rng.uniform(60, 95), "core_deposits_to_total_deposits": rng.uniform(80, 95),
            "unrealized_loss_to_tier1": rng.uniform(0, 20), "cre_to_capital": rng.uniform(50, 250),
            "construction_to_capital": rng.uniform(5, 60), "reciprocal_cap_used": rng.uniform(0, 30)}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "t.duckdb"
    monkeypatch.setenv("DUCKDB_PATH", str(path))
    c = db.connect(str(path))
    rng = random.Random(3)
    for i in range(150):
        rssd, assets = str(1000 + i), [150_000, 600_000, 3_000_000][i % 3] * rng.uniform(0.8, 1.2)
        q2 = _ordinary(rng, assets)
        q3 = {**q2, "total_assets": assets * 1.01}
        db.upsert_institution_meta(c, db.InstitutionMeta(rssd, f"Bank {i}", "Town", "KS", str(50_000 + i), assets, Q3))
        db.upsert_snapshot(c, db.Snapshot(rssd, Q2, None, q2, None))
        db.upsert_snapshot(c, db.Snapshot(rssd, Q3, None, q3, None))
    planted = {
        "9001": ("Weak Bank", {"tier1_leverage_ratio": 4.2, "total_rbc_ratio": 7.5, "tier1_rbc_ratio": 6.5,
                               "cet1_ratio": 6.5, "roaa": -2.0}),
        "9002": ("Run Bank", {R.DEPOSITS_RAW_KEY: 300_000.0, R.UNINSURED_RAW_KEY: 50_000.0}),
        "9003": ("Grace Bank", {"tier1_leverage_ratio": 8.5, "tier1_rbc_ratio": None, "total_rbc_ratio": None,
                                "cet1_ratio": None}),
        "9004": ("Trust Co", {R.DEPOSITS_RAW_KEY: 1_000.0, "net_loans_leases_to_deposits": None,
                              "tier1_leverage_ratio": 95.0}),
    }
    for rssd, (name, over) in planted.items():
        base = _ordinary(rng, 500_000)
        db.upsert_institution_meta(c, db.InstitutionMeta(rssd, name, "Town", "KS", rssd, 500_000, Q3))
        db.upsert_snapshot(c, db.Snapshot(rssd, Q2, None, base, None))
        db.upsert_snapshot(c, db.Snapshot(rssd, Q3, None, {**base, **over}, None))
    db.upsert_institution_meta(c, db.InstitutionMeta("9005", "Late Bank", "Town", "KS", "9005", 400_000, Q2))
    db.upsert_snapshot(c, db.Snapshot("9005", Q2, None, _ordinary(rng, 400_000), None))
    db.log_alert(c, "1007", Q3, "TIER3", "RESTATEMENT_SUB_FLOOR", {})
    return c


def _by_name(banks):
    return {b.name: b for b in banks}


def test_screens_trends_and_ranking(conn, monkeypatch):
    from scripts import send_risk_tests as S
    monkeypatch.setattr(S.enforcement, "load_fdic", lambda: ([], ""))
    banks, missing, _actions = S.load(conn, Q3)
    ranked = RT.run(banks)
    b = _by_name(ranked)
    assert any(s.startswith("Below well capitalized: Undercapitalized") for s in b["Weak Bank"].screens)
    assert any(s.startswith("Losing money") for s in b["Weak Bank"].screens)
    assert any(t.startswith("Deposit outflow 10%+") for t in b["Run Bank"].trends)
    assert any(t.startswith("Uninsured outflow 15%+") for t in b["Run Bank"].trends)
    assert not any(s.startswith("Below well capitalized") for s in b["Grace Bank"].screens)    # CBLR grace band
    assert "Thin capital buffer: CBLR grace band, leverage 8.50% (9% line)" in b["Grace Bank"].screens
    assert any("Tier 1 leverage 4.20% (#1 of" in o for o in b["Weak Bank"].outliers)
    assert b["Trust Co"].specialty and b["Trust Co"].score is None and b["Trust Co"].rank is None
    assert b["Weak Bank"].rank <= 5                                     # the composite puts it near the top
    assert b["Bank 7"].refiled
    assert missing == [("9005", "Late Bank", "KS")]
    # V2-formula trends stay off while either quarter predates the current formulas
    assert not any(t.startswith("Margin down") for x in ranked for t in x.trends)


def test_percentiles_rank_riskiest_highest():
    banks = [RT.Bank(str(i), f"B{i}", "", "", "", {}) for i in range(4)]
    p = RT._percentiles([(banks[0], 1.0), (banks[1], 2.0), (banks[2], 2.0), (banks[3], 9.0)], True)
    assert p["3"] == 100 and p["0"] == 25 and p["1"] == p["2"] == 62.5


def test_script_writes_a_workbook(conn, tmp_path, monkeypatch):
    from scripts import send_risk_tests as S
    monkeypatch.setattr(S.enforcement, "load_fdic", lambda: ([], ""))
    out = tmp_path / "risk.xlsx"
    conn.close()
    S.main("2026-09-30", "--out", str(out))
    wb = openpyxl.load_workbook(io.BytesIO(out.read_bytes()))
    assert wb.sheetnames == ["Summary", "Risk Ranking", "Screens", "Trends", "Peer Outliers", "Data Quality",
                             "Lists", "Test Guide"]
    ranking = list(wb["Risk Ranking"].iter_rows(values_only=True))
    assert ranking[0][:3] == ("Rank", "Institution", "City") and ranking[1][0] == 1
    assert len(ranking) == 1 + 154
    assert any(r[0] == "Trust Co" or r[1] == "Trust Co" for r in ranking)
