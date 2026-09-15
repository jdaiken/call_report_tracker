from datetime import date

from ffiec_monitor import db as monitor_db
from historical_risk import db as hdb
from historical_risk.field_stats import (
    before_after_snapshots,
    order_frequency_by_year_and_type,
    order_type_ratio_crosstab,
)


def _seed(conn):
    conn.executemany(
        "INSERT INTO enforcement_orders "
        "(id_rssd, fdic_cert, order_type, status, effective_date, terminated_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("1", "C1", "SAFETY_SOUNDNESS", "ACTIVE", date(2020, 6, 30), None),
            ("2", "C2", "AML_BSA", "TERMINATED", date(2021, 3, 31), date(2022, 3, 31)),
        ],
    )

    ratio_cols = (
        "id_rssd, period_end, bank_name, peer_group_id, tier1_leverage_ratio, tier1_rbc_ratio, "
        "total_rbc_ratio, cet1_ratio, roaa, net_interest_margin, noncurrent_loans_oreo_ratio, "
        "efficiency_ratio, net_loans_leases_to_deposits, net_noncore_funding_dependence, "
        "charge_off_ratio, loan_growth_rate, capital_growth_rate, source_file, loaded_at"
    )
    rows = [
        # Bank 1 around its 2020-06-30 order: T-4, T-1, T, T+4.
        ("1", date(2019, 6, 30), "Bank 1", "PG1", 6.0, 9.0, 11.0, 8.0, 1.2, 3.5, 1.0, 60.0, 80.0, 20.0, 0.1, 5.0, 4.0, "f.zip"),
        ("1", date(2020, 3, 31), "Bank 1", "PG1", 4.5, 7.5, 9.5, 6.0, -0.5, 3.0, 2.5, 68.0, 90.0, 25.0, 0.6, 2.0, 1.0, "f.zip"),
        ("1", date(2020, 6, 30), "Bank 1", "PG1", 4.0, 7.0, 9.0, 5.5, -1.0, 2.8, 3.0, 70.0, 95.0, 28.0, 0.9, 1.0, 0.5, "f.zip"),
        ("1", date(2021, 6, 30), "Bank 1", "PG1", 5.0, 8.0, 10.0, 6.5, 0.4, 3.1, 1.8, 62.0, 85.0, 22.0, 0.3, 3.0, 2.0, "f.zip"),
        # Bank 2 near its 2021-03-31 order (T only, to prove partial coverage doesn't crash).
        ("2", date(2021, 3, 31), "Bank 2", "PG2", 8.0, 11.0, 13.0, 10.0, 0.8, 3.4, 0.5, 55.0, 70.0, 15.0, 0.05, 6.0, 5.0, "f.zip"),
    ]
    conn.executemany(
        f"INSERT INTO ubpr_ratios ({ratio_cols}) VALUES ({', '.join(['?'] * 19)})",
        [r + (None,) for r in rows],  # loaded_at
    )


def test_order_frequency_by_year_and_type():
    conn = monitor_db.connect(":memory:")
    hdb.ensure_schema(conn)
    _seed(conn)

    df = order_frequency_by_year_and_type(conn)
    d = {(int(r.year), r.order_type): r.order_count for r in df.itertuples()}
    assert d[(2020, "SAFETY_SOUNDNESS")] == 1
    assert d[(2021, "AML_BSA")] == 1


def test_before_after_snapshots_finds_all_four_offsets_for_bank_1():
    conn = monitor_db.connect(":memory:")
    hdb.ensure_schema(conn)
    _seed(conn)

    df = before_after_snapshots(conn)
    bank1 = df[df["id_rssd"] == "1"]
    assert set(bank1["label"]) == {"T-4", "T-1", "T", "T+4"}

    t_row = bank1[bank1["label"] == "T"].iloc[0]
    assert t_row["period_end"].date() == date(2020, 6, 30)
    assert t_row["tier1_leverage_ratio"] == 4.0


def test_order_type_ratio_crosstab_groups_by_order_type():
    conn = monitor_db.connect(":memory:")
    hdb.ensure_schema(conn)
    _seed(conn)

    df = order_type_ratio_crosstab(conn)
    types = set(df["order_type"])
    assert types == {"SAFETY_SOUNDNESS", "AML_BSA"}
    assert (df.set_index("order_type").loc["SAFETY_SOUNDNESS", "n"]) == 1
