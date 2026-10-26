"""TEST: find FFIEC CDR's UBPR report-cycle ids by period (public pages, no auth)."""
import re
import sys

import requests

RSSD = sys.argv[1] if len(sys.argv) > 1 else "480228"
s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0"
found = []
for cyc in list(range(int(sys.argv[2]) if len(sys.argv) > 2 else 100, int(sys.argv[3]) if len(sys.argv) > 3 else 170)):
    url = f"https://cdr.ffiec.gov/Public/Reports/UbprReport.aspx?rptCycleIds={cyc}&rptid=283&idrssd={RSSD}"
    try:
        t = s.get(url, timeout=30).text
    except Exception as e:
        print(cyc, "ERR", e)
        continue
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}) Uniform Bank Performance Report", t)
    found.append(f"{cyc}={m.group(1) if m else 'none'}")
print("; ".join(found))
print(f"::notice title=UBPR cycles::{'; '.join(found)[:950]}")
