import zipfile
from datetime import date
from pathlib import Path

import duckdb

from historical_risk import db as hdb
from historical_risk.cleanup import clean_one_file
from historical_risk.field_mapping import FieldMapping



# Columns for the ratios EXPECTED_RATIO_LOGICAL_NAMES needs beyond leverage/cet1/roaa
# above -- clean_one_file requires a mapping row for every one of them (it fails
# loudly otherwise, see cleanup.py), so the synthetic fixture below must supply
# all of them even though these tests only assert on a few.
_OTHER_RATIO_COLS = [
    "tier1_rbc_ratio", "total_rbc_ratio", "net_interest_margin",
    "noncurrent_loans_oreo_ratio", "efficiency_ratio", "net_loans_leases_to_deposits",
    "net_noncore_funding_dependence", "charge_off_ratio", "loan_growth_rate",
    "capital_growth_rate",
]


def _synthetic_mappings() -> list[FieldMapping]:
    mappings = [
        FieldMapping("id_rssd", "RSSD_COL", "identifier", None, None, True, ""),
        FieldMapping("period_end", "PERIOD_COL", "identifier", None, None, True, ""),
        FieldMapping("bank_name", "NAME_COL", "identifier", None, None, True, ""),
        FieldMapping("peer_group_id", "PEER_COL", "identifier", None, None, True, ""),
        FieldMapping("tier1_leverage_ratio", "LEV_COL", "Capital", date(2012, 1, 1), None, True, ""),
        FieldMapping("cet1_ratio", "CET1_COL", "Capital", date(2015, 1, 1), None, True, ""),
        FieldMapping("roaa", "ROAA_COL", "Earnings", date(2012, 1, 1), None, True, ""),
    ]
    for name in _OTHER_RATIO_COLS:
        mappings.append(FieldMapping(name, f"{name.upper()}_COL", "Other", date(2012, 1, 1), None, True, ""))
    return mappings


def _write_raw_zip(tmp_path: Path, rows: list[dict]) -> Path:
    header = ["RSSD_COL", "PERIOD_COL", "NAME_COL", "PEER_COL", "LEV_COL", "CET1_COL", "ROAA_COL"]
    header += [f"{name.upper()}_COL" for name in _OTHER_RATIO_COLS]
    lines = ["\t".join(header)]
    for r in rows:
        lines.append("\t".join(str(r.get(h, "")) for h in header))

    tsv_path = tmp_path / "raw.txt"
    tsv_path.write_text("\n".join(lines))

    zip_path = tmp_path / "raw.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(tsv_path, arcname="raw.txt")
    return zip_path


def test_clean_one_file_writes_expected_rows(tmp_path):
    rows = [
        {
            "RSSD_COL": "123456", "PERIOD_COL": "2016-03-31", "NAME_COL": "Test Bank",
            "PEER_COL": "PG1", "LEV_COL": "9.50", "CET1_COL": "11.20", "ROAA_COL": "1.10",
        },
    ]
    zip_path = _write_raw_zip(tmp_path, rows)

    conn = duckdb.connect(":memory:")
    hdb.ensure_schema(conn)

    written = clean_one_file(conn, zip_path, _synthetic_mappings())
    assert written == 1

    row = conn.execute(
        "SELECT id_rssd, period_end, bank_name, peer_group_id, tier1_leverage_ratio, cet1_ratio, roaa "
        "FROM ubpr_ratios"
    ).fetchone()
    assert row == ("123456", date(2016, 3, 31), "Test Bank", "PG1", 9.50, 11.20, 1.10)


def test_clean_one_file_leaves_cet1_null_before_2015_not_zero_filled(tmp_path):
    rows = [
        {
            "RSSD_COL": "999", "PERIOD_COL": "2013-06-30", "NAME_COL": "Old Bank",
            "PEER_COL": "PG2", "LEV_COL": "8.00", "CET1_COL": "0.00", "ROAA_COL": "0.90",
        },
    ]
    zip_path = _write_raw_zip(tmp_path, rows)

    conn = duckdb.connect(":memory:")
    hdb.ensure_schema(conn)
    clean_one_file(conn, zip_path, _synthetic_mappings())

    cet1 = conn.execute("SELECT cet1_ratio FROM ubpr_ratios WHERE id_rssd = '999'").fetchone()[0]
    assert cet1 is None, "cet1_ratio doesn't exist pre-2015 -- must be NULL, never 0.00 from the raw file"


def test_clean_one_file_upserts_on_conflict(tmp_path):
    rows = [
        {
            "RSSD_COL": "1", "PERIOD_COL": "2020-12-31", "NAME_COL": "Bank A",
            "PEER_COL": "PG1", "LEV_COL": "7.00", "CET1_COL": "9.00", "ROAA_COL": "0.50",
        },
    ]
    zip_path = _write_raw_zip(tmp_path, rows)

    conn = duckdb.connect(":memory:")
    hdb.ensure_schema(conn)
    clean_one_file(conn, zip_path, _synthetic_mappings())

    rows[0]["LEV_COL"] = "7.25"
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    zip_path2 = _write_raw_zip(second_dir, rows)
    clean_one_file(conn, zip_path2, _synthetic_mappings())

    count, lev = conn.execute(
        "SELECT count(*), max(tier1_leverage_ratio) FROM ubpr_ratios WHERE id_rssd = '1'"
    ).fetchone()
    assert count == 1
    assert lev == 7.25
