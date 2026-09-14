#!/usr/bin/env python3
"""One-off: clear poll_state so the next run's retrieve_filer_statuses call uses
FIRST_POLL_SENTINEL again instead of a cursor already advanced by an earlier run
that crashed *after* the cursor was set but *before* it finished processing --
without this, a re-run can silently see zero "changed" institutions and falsely
look like a clean pass. Not part of the regular pipeline; delete once no longer
needed for this kind of re-test.
"""
from __future__ import annotations

from ffiec_monitor import config, db

if __name__ == "__main__":
    conn = db.connect(config.get_duckdb_path())
    before = conn.execute("SELECT count(*) FROM poll_state").fetchone()[0]
    conn.execute("DELETE FROM poll_state")
    print(f"Cleared poll_state ({before} row(s) removed) -- next run starts from FIRST_POLL_SENTINEL.")
