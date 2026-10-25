"""TEST: run the new-filing level screen (ffiec_monitor/screen.py) over every bank's
latest stored snapshot and print how many would be flagged, per rule -- counts only,
no email, no FFIEC API call. Used to calibrate the screen's ranges on real data.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db
from ffiec_monitor.config import get_duckdb_path
from ffiec_monitor.screen import screen, severity


def main() -> None:
    conn = db.connect(get_duckdb_path())
    rows = conn.execute(
        "SELECT ratios FROM (SELECT ratios, ROW_NUMBER() OVER (PARTITION BY id_rssd ORDER BY period_end DESC) rn "
        "FROM call_report_snapshots) WHERE rn = 1").fetchall()
    per_rule, red, amber_only = Counter(), 0, 0
    for (raw,) in rows:
        flags = screen(json.loads(raw) if raw else None)
        for f in flags:
            per_rule[f"{f.level}:{f.key}"] += 1
        c, w = severity(flags)
        red += bool(c)
        amber_only += bool(w and not c)
    n = len(rows)
    line = (f"{n} banks; red-flagged {red} ({red / max(n, 1):.0%}); amber only {amber_only} "
            f"({amber_only / max(n, 1):.0%}). By rule: " + ", ".join(f"{k} {v}" for k, v in sorted(per_rule.items())))
    print(line)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Screen stats::{line[:900]}")


if __name__ == "__main__":
    main()
