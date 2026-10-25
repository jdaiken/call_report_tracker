"""TEST: pull ONE Call Report facsimile (public data) and print its raw shape --
length, first characters, line count, delimiters -- so _parse_sdf can be fixed
against a real sample. No email, no DB writes. Prints GitHub annotations.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffiec_monitor import db, ffiec_client
from ffiec_monitor.config import FFIECConfig, get_duckdb_path


def note(title: str, msg: str) -> None:
    print(f"{title}: {msg}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title={title}::{msg[:950].replace(chr(10), ' | ').replace(chr(13), '')}")


def main() -> None:
    conn = db.connect(get_duckdb_path())
    rssd, period = conn.execute(
        "SELECT id_rssd, period_end FROM call_report_snapshots ORDER BY period_end DESC, checked_at DESC LIMIT 1").fetchone()
    client = ffiec_client.FFIECClient(FFIECConfig.from_env())
    payload = client._get("RetrieveFacsimile", extra_headers={
        "dataSeries": "Call", "reportingPeriodEndDate": period.strftime(ffiec_client.DATE_FMT),
        "fiIdType": "ID_RSSD", "fiId": str(rssd), "facsimileFormat": "SDF"})
    note("Probe target", f"RSSD {rssd}, period {period}, payload type {type(payload).__name__}")
    raw = ffiec_client._unwrap_facsimile_payload(payload, "FacsimileFile")
    data = ffiec_client._decode_byte_field(raw)
    text = data.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    note("Probe size", f"{len(data)} bytes, {len(lines)} lines, tabs {text.count(chr(9))}, semicolons {text.count(';')}, "
                       f"commas {text.count(',')}, pipes {text.count('|')}")
    note("Probe head", repr(text[:700]))
    note("Probe line 2-4", repr("\n".join(lines[1:4])[:700]))
    note("Probe parsed", json.dumps(dict(list(ffiec_client._parse_sdf(raw).items())[:8]))[:700])


if __name__ == "__main__":
    main()
