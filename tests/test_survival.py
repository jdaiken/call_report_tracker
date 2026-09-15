from datetime import date

import duckdb

from ffiec_monitor import db as monitor_db
from historical_risk.survival import fit_by_order_type, load_order_durations


def _seed_orders(conn):
    conn.executemany(
        "INSERT INTO enforcement_orders "
        "(id_rssd, fdic_cert, order_type, status, effective_date, terminated_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            # Terminated after 1 year -- an observed event.
            ("1", "C1", "SAFETY_SOUNDNESS", "TERMINATED", date(2018, 1, 1), date(2019, 1, 1)),
            # Still active -- censored, not "cured", not dropped.
            ("2", "C2", "SAFETY_SOUNDNESS", "ACTIVE", date(2022, 1, 1), None),
            ("3", "C3", "AML_BSA", "TERMINATED", date(2020, 1, 1), date(2021, 6, 30)),
        ],
    )


def test_load_order_durations_marks_open_orders_censored():
    conn = monitor_db.connect(":memory:")
    _seed_orders(conn)

    df = load_order_durations(conn, as_of=date(2024, 1, 1))
    assert len(df) == 3

    censored = df[df["id_rssd"] == "2"].iloc[0]
    assert censored["event_observed"] == 0
    assert censored["duration_days"] > 0  # measured up to as_of, not dropped/zeroed

    terminated = df[df["id_rssd"] == "1"].iloc[0]
    assert terminated["event_observed"] == 1
    assert terminated["duration_days"] == 365


def test_fit_by_order_type_produces_one_fitter_and_summary_per_type():
    conn = monitor_db.connect(":memory:")
    _seed_orders(conn)
    durations = load_order_durations(conn, as_of=date(2024, 1, 1))

    fitters, summaries = fit_by_order_type(durations)

    assert set(fitters.keys()) == {"SAFETY_SOUNDNESS", "AML_BSA"}
    by_type = {s.order_type: s for s in summaries}
    assert by_type["SAFETY_SOUNDNESS"].n_orders == 2
    assert by_type["AML_BSA"].n_orders == 1

    # Fractions still active should be non-increasing with horizon.
    for s in summaries:
        assert s.frac_active_at_1y >= s.frac_active_at_2y >= s.frac_active_at_4y
        assert 0.0 <= s.frac_active_at_4y <= s.frac_active_at_1y <= 1.0
