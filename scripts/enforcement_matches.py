"""List every bank in a logged window that matches a public enforcement action
(no facsimile pulls, no email). Prints GitHub annotations, a few matches per line.

  python scripts/enforcement_matches.py "2026-10-18 21:00" "2026-10-26 00:00"
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, enforcement
from ffiec_monitor.config import get_duckdb_path
from ffiec_monitor.report import _bank


def main(start: str, end: str) -> None:
    conn = db.connect(get_duckdb_path())
    rows = conn.execute("SELECT DISTINCT id_rssd, period_end FROM alert_log WHERE logged_at >= ? AND logged_at < ?",
                        [datetime.fromisoformat(start), datetime.fromisoformat(end)]).fetchall()
    banks = {}
    for rssd, period_end in rows:
        meta = db.get_institution_meta(conn, rssd)
        if meta:
            banks[rssd] = _bank(meta, period_end)
    src = enforcement.fetch_all(datetime.now(timezone.utc).date())
    matches = enforcement.match(list(banks.values()), src.actions)
    lines = [f"{'PENALTY' if enforcement.is_penalty(m.action) else 'ORDER'} | {m.bank.name} ({m.bank.location}, "
             f"{m.bank.assets}) | {m.action.agency} {m.action.action} | since "
             f"{m.action.start:%Y-%m-%d}" if m.action.start else f"{m.bank.name}: {m.action.agency} {m.action.action}"
             for m in matches]
    print(f"{len(banks)} banks, {len(matches)} matches, sources {src.loaded}, errors {src.errors}")
    for ln in lines:
        print(ln)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Summary::{len(banks)} banks, {len(matches)} matches, sources {src.loaded}")
        chunk, n = [], 0
        for ln in lines:
            if sum(len(x) + 3 for x in chunk) + len(ln) > 900:
                n += 1
                print(f"::notice title=Matches {n}::{' || '.join(chunk)}")
                chunk = []
            chunk.append(ln)
        if chunk:
            print(f"::notice title=Matches {n + 1}::{' || '.join(chunk)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
