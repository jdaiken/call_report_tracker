#!/usr/bin/env python3
"""Resolve risk_rankings rows (name + state, no RSSD) using the Panel of Reporters
(POR) file from an FFIEC CDR bulk Call Report download -- the full roster of every
current filer (RSSD, name, city, state, FDIC cert), no API token needed.

This is the preferred path when you have a recent bulk download on hand (e.g. the
quarterly "FFIEC CDR Call Bulk All Schedules" zip): it's instant, offline, and the
identical roster the live API's RetrievePanelOfReporters would return for the same
period. Use scripts/resolve_risk_rankings.py instead once this period's bulk file
has aged out and you need a fresher roster from the live API.

See ffiec_monitor/matching.py for the matching rules (conservative: only exact
normalized name+state matches auto-assign; everything else is reported for manual
review, never guessed).

Usage:
    python scripts/resolve_from_bulk_por.py path/to/bulk.zip [--db path.duckdb]
    python scripts/resolve_from_bulk_por.py "path/to/FFIEC CDR Call Bulk POR 06302026.txt"
"""
from __future__ import annotations

import argparse
import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path
from ffiec_monitor.matching import PanelEntry, print_report, resolve

# An ambiguous name collision (e.g. two "Farmers State Bank, IA") can often still be
# broken safely using a completely independent signal: total assets. If the risk
# ranking's reported assets match exactly one candidate within this tolerance, and
# every other candidate is far off, auto-assign -- otherwise leave it for review.
ASSET_MATCH_TOLERANCE = 0.02
ASSET_RUNNER_UP_MIN_RATIO = 5.0  # runner-up candidate's error must be >=5x the best match's


def _load_por_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            por_names = [n for n in z.namelist() if "por" in n.lower() or "panel of reporters" in n.lower()]
            if not por_names:
                raise SystemExit(f"No Panel of Reporters file found inside {path}")
            with z.open(por_names[0]) as f:
                text = f.read().decode("utf-8-sig")
    else:
        text = path.read_text(encoding="utf-8-sig")

    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    return list(reader)


def _parse_field(raw: str) -> float | None:
    raw = raw.strip().rstrip("%")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _load_rc_total_assets(path: Path, wanted_rssds: set[str]) -> dict[str, float]:
    """RCON2170/RCFD2170 (total assets, thousands of dollars) for specific RSSDs
    only -- used to break name-collision ties, not for anything else here."""
    if path.suffix.lower() != ".zip":
        return {}
    with zipfile.ZipFile(path) as z:
        rc_names = [n for n in z.namelist() if "schedule rc " in n.lower()]
        if not rc_names:
            return {}
        with z.open(rc_names[0]) as f:
            text = f.read().decode("utf-8-sig")
    lines = text.splitlines()
    codes = lines[0].split("\t")
    idx = {c.strip('"'): i for i, c in enumerate(codes)}
    rcon_i, rcfd_i = idx.get("RCON2170"), idx.get("RCFD2170")
    out: dict[str, float] = {}
    for line in lines[1:]:
        cells = line.split("\t")
        rssd = cells[0].strip('"')
        if rssd not in wanted_rssds:
            continue
        value = _parse_field(cells[rcfd_i]) if rcfd_i is not None else None
        if value is None and rcon_i is not None:
            value = _parse_field(cells[rcon_i])
        if value is not None:
            out[rssd] = value * 1000  # thousands -> dollars, matching risk_rankings.total_assets
    return out


def _pick_by_assets(
    reported: float | None, candidate_rssds: list[str], candidate_assets: dict[str, float],
) -> str | None:
    """Among a name collision's candidates, pick the one whose real reported
    assets are close to the risk ranking's reported assets -- only if it's an
    unambiguous winner (within tolerance, and every other candidate far off)."""
    if reported is None or reported == 0:
        return None
    diffs = []
    for rssd in candidate_rssds:
        actual = candidate_assets.get(rssd)
        if actual is None:
            continue
        diffs.append((abs(actual - reported) / reported, rssd))
    if not diffs:
        return None
    diffs.sort()
    best_diff, best_rssd = diffs[0]
    if best_diff <= ASSET_MATCH_TOLERANCE / 4:
        return best_rssd  # near-exact match to the reported dollar -- safe regardless of runner-up
    if best_diff > ASSET_MATCH_TOLERANCE:
        return None
    if len(diffs) > 1 and diffs[1][0] < best_diff * ASSET_RUNNER_UP_MIN_RATIO:
        return None  # runner-up not clearly worse -- ambiguous
    return best_rssd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bulk_path", type=Path, help="bulk download .zip, or the extracted POR .txt directly")
    parser.add_argument("--db", default=get_duckdb_path())
    args = parser.parse_args()

    rows = _load_por_rows(args.bulk_path)
    panel = [
        PanelEntry(
            id_rssd=row["IDRSSD"].strip(),
            name=row["Financial Institution Name"].strip(),
            state=row["Financial Institution State"].strip(),
        )
        for row in rows
        if row.get("IDRSSD")
    ]
    print(f"Panel of reporters: {len(panel)} institutions")

    conn = db.connect(args.db)
    unresolved_with_assets = db.unresolved_risk_rankings_with_assets(conn)
    print(f"risk_rankings rows needing resolution: {len(unresolved_with_assets)}")

    unresolved_rows = [(rid, name, state) for rid, name, state, _assets in unresolved_with_assets]
    reported_assets = {rid: assets for rid, _name, _state, assets in unresolved_with_assets}

    result = resolve(unresolved_rows, panel)
    for row_id, rssd in result.resolved:
        conn.execute("UPDATE risk_rankings SET id_rssd = ? WHERE id = ?", [rssd, row_id])

    still_ambiguous = []
    if result.ambiguous:
        all_candidate_rssds = {r for _rid, _n, _s, rssds in result.ambiguous for r in rssds}
        candidate_assets = _load_rc_total_assets(args.bulk_path, all_candidate_rssds)
        asset_resolved = 0
        for row_id, bank_name, state, rssds in result.ambiguous:
            reported = reported_assets.get(row_id)
            picked = _pick_by_assets(reported, rssds, candidate_assets)
            if picked is not None:
                conn.execute("UPDATE risk_rankings SET id_rssd = ? WHERE id = ?", [picked, row_id])
                asset_resolved += 1
            else:
                still_ambiguous.append((row_id, bank_name, state, rssds))
        if asset_resolved:
            print(f"Disambiguated {asset_resolved} name collisions using reported total assets "
                  f"(within {ASSET_MATCH_TOLERANCE:.0%}, with a clear runner-up gap)")
        result.ambiguous = still_ambiguous

    print_report(result)


if __name__ == "__main__":
    main()
