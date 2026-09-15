"""Cleanup step: raw UBPR bulk pull -> clean, logically-named `ubpr_ratios` table
(spec §10). Run once per pulled quarter, right after pull.py, before field_stats.py
or survival.py touch anything.

**File-layout confidence: UNCONFIRMED.** This build sandbox has no network access
to cdr.ffiec.gov (see pull.py's docstring), so `_read_raw_rows()` below has never
been run against a real UBPR_RATIO_FOUR TSV -- it assumes the common FFIEC bulk
layout (one header row of field codes, one row per institution per period, a
period-end column identifying which of the four quarters a row belongs to).
**Call `inspect_raw_file()` on the very first real downloaded file before trusting
any of this** -- it just prints the header so you can confirm-or-fix the column
names in config/ubpr_field_mapping.csv and, if the real layout turns out to be
wide-per-period instead of long, fix `_read_raw_rows()`'s reshape. This mirrors
how ffiec_monitor/ratios.py and README's "Known limitations" flag confidence
per-field rather than asserting false certainty.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb

from .field_mapping import EXPECTED_RATIO_LOGICAL_NAMES, FieldMapping, active_mapping_for_period

logger = logging.getLogger(__name__)


def inspect_raw_file(zip_path: Path, max_lines: int = 3) -> None:
    """Print the header + a few sample lines of a raw pull's TSV member, so a
    human can confirm the assumed layout against config/ubpr_field_mapping.csv's
    raw_field_id column before running the real cleanup (spec §10)."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".txt", ".tsv", ".csv"))]
        if not names:
            raise ValueError(f"No .txt/.tsv/.csv member found in {zip_path}: {zf.namelist()}")
        with zf.open(names[0]) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            for i, line in enumerate(text):
                print(f"[{names[0]}:{i}] {line.rstrip()[:500]}")
                if i >= max_lines:
                    break


def _parse_period_end(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse period_end value {raw!r} -- check the raw file's date format")


def _read_raw_rows(zip_path: Path) -> Iterator[dict[str, Any]]:
    """Yields one dict per (institution, period) row from a raw pull's TSV member,
    keyed by raw column name. See module docstring re: layout confidence."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".txt", ".tsv", ".csv"))]
        if not names:
            raise ValueError(f"No .txt/.tsv/.csv member found in {zip_path}: {zf.namelist()}")
        with zf.open(names[0]) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            reader = csv.DictReader(text, delimiter="\t")
            yield from reader


def _num(raw: Any) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().rstrip("%")
    if not s or s.upper() in {"NA", "N/A", "NULL"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean_one_file(
    conn: duckdb.DuckDBPyConnection,
    zip_path: Path,
    mappings: list[FieldMapping],
    id_col: str = "id_rssd",
    period_col: str = "period_end",
    name_col: str = "bank_name",
    peer_col: str = "peer_group_id",
) -> int:
    """Reads one raw pull file, selects only the ~12-15 mapped fields + identifiers
    (spec §10 -- everything else is dropped here, not carried downstream), resolves
    each logical name's raw_field_id against the mapping active for that row's
    period_end (spec §11), and upserts into ubpr_ratios. Returns rows written.

    `id_col`/`period_col`/`name_col`/`peer_col` are the *logical* names for the
    four identifier rows in config/ubpr_field_mapping.csv -- pass the real raw
    column names once confirmed (see inspect_raw_file()); left as logical-name
    defaults here since the raw ones are still "TBD" in the shipped config.
    """
    id_map = {m.logical_name: m for m in mappings if m.ubpr_page == "identifier"}
    ratio_map = {m.logical_name: m for m in mappings if m.ubpr_page != "identifier"}

    missing = [name for name in EXPECTED_RATIO_LOGICAL_NAMES if name not in ratio_map]
    if missing:
        raise ValueError(
            f"config/ubpr_field_mapping.csv is missing a row for: {', '.join(missing)} -- "
            "ubpr_ratios (historical_risk/db.py) has a column for each of "
            "EXPECTED_RATIO_LOGICAL_NAMES and clean_one_file needs a mapping row for all of them."
        )

    rows_written = 0
    for raw in _read_raw_rows(zip_path):
        period_end = _parse_period_end(raw[id_map[period_col].raw_field_id])
        active = active_mapping_for_period(list(ratio_map.values()), period_end)

        values: dict[str, float | None] = {}
        for logical_name, mapping in ratio_map.items():
            if logical_name not in active:
                values[logical_name] = None  # not active this period -- spec §11, never zero-fill
                continue
            values[logical_name] = _num(raw.get(mapping.raw_field_id))

        conn.execute(
            """
            INSERT INTO ubpr_ratios (
                id_rssd, period_end, bank_name, peer_group_id,
                tier1_leverage_ratio, tier1_rbc_ratio, total_rbc_ratio, cet1_ratio,
                roaa, net_interest_margin, noncurrent_loans_oreo_ratio, efficiency_ratio,
                net_loans_leases_to_deposits, net_noncore_funding_dependence,
                charge_off_ratio, loan_growth_rate, capital_growth_rate,
                camels_composite, source_file, loaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, now())
            ON CONFLICT (id_rssd, period_end) DO UPDATE SET
                bank_name = excluded.bank_name,
                peer_group_id = excluded.peer_group_id,
                tier1_leverage_ratio = excluded.tier1_leverage_ratio,
                tier1_rbc_ratio = excluded.tier1_rbc_ratio,
                total_rbc_ratio = excluded.total_rbc_ratio,
                cet1_ratio = excluded.cet1_ratio,
                roaa = excluded.roaa,
                net_interest_margin = excluded.net_interest_margin,
                noncurrent_loans_oreo_ratio = excluded.noncurrent_loans_oreo_ratio,
                efficiency_ratio = excluded.efficiency_ratio,
                net_loans_leases_to_deposits = excluded.net_loans_leases_to_deposits,
                net_noncore_funding_dependence = excluded.net_noncore_funding_dependence,
                charge_off_ratio = excluded.charge_off_ratio,
                loan_growth_rate = excluded.loan_growth_rate,
                capital_growth_rate = excluded.capital_growth_rate,
                source_file = excluded.source_file,
                loaded_at = excluded.loaded_at
            """,
            [
                raw[id_map[id_col].raw_field_id],
                period_end,
                raw.get(id_map[name_col].raw_field_id) if name_col in id_map else None,
                raw.get(id_map[peer_col].raw_field_id) if peer_col in id_map else None,
                values["tier1_leverage_ratio"], values["tier1_rbc_ratio"],
                values["total_rbc_ratio"], values["cet1_ratio"],
                values["roaa"], values["net_interest_margin"],
                values["noncurrent_loans_oreo_ratio"], values["efficiency_ratio"],
                values["net_loans_leases_to_deposits"], values["net_noncore_funding_dependence"],
                values["charge_off_ratio"], values["loan_growth_rate"], values["capital_growth_rate"],
                zip_path.name,
            ],
        )
        rows_written += 1

    logger.info("Cleaned %d rows from %s", rows_written, zip_path.name)
    return rows_written
