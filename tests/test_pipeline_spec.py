import io
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_pipeline_spec as b  # noqa: E402

Q3 = date(2026, 9, 30)


def _tsv(rows):
    return "\n".join("\t".join(r) for r in rows)


def _zip():
    por = _tsv([["IDRSSD", "FDIC Certificate Number", "Financial Institution Name", "Financial Institution State",
                 "Financial Institution Filing Type"],
                ["480228", "3511", "Big Bank", "NC", "031"], ["999", "12345", "Small Bank", "IA", "051"]])
    rc = _tsv([["IDRSSD", "RCFD2170", "RCON2170", "RCON2200", "RCFN2200", "RCON5597", "RCONF051", "RCONF052"],
               ["", "d", "", "", "", "", "", ""],
               ["480228", "1000000", "", "500000", "100000", "200000", "", ""],
               ["999", "", "50000", "", "", "", "3000", "4"]])
    rcr = _tsv([["IDRSSD", "RCFA7206", "RCFA7204", "RCOA7204", "RCOALE74"], ["", "d", "", "", ""],
                ["480228", "11.2%", "8.5%", "", ""], ["999", "", "", "10.1234%", "1"]])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("FFIEC CDR Call Bulk POR 09302026.txt", por)
        z.writestr("FFIEC CDR Call Schedule RC 09302026.txt", rc)
        z.writestr("FFIEC CDR Call Schedule RCRI 09302026.txt", rcr)
    return buf.getvalue()


def test_bulk_to_rows():
    panel, conc = b.parse_bulk(_zip(), b.wanted_codes())
    rows = {r["FRS ID"]: r for r in b.build_rows({Q3: (panel, conc)}, ({"480228": 1073757}, {"12345": 0}), {})}
    big, small = rows[480228], rows[999]
    assert big["FORM"] == "031" and big["CBLR"] == "N" and big["HOLD TYPE"] == 2
    assert big["RC_2170"] == 1000000 and big["RC_2200"] == 600000           # domestic + foreign
    assert big["Uninsured deposits (estimated, all banks)"] == 200000       # 5597 where reported
    assert big["RCR_7206"] == 11.2 and big["RCR_7205"] is None              # blank, never 0
    assert small["CBLR"] == "Y" and small["PARENT NUMBER"] == 0 and small["HOLD TYPE"] == 0
    assert small["Uninsured deposits (estimated, all banks)"] == 3000 - 250 * 4
    assert small["RCR_7204"] == 10.1234 and small["RC_5597"] is None
    assert small["TOTAL ASSETS DOLLARS IN MILLIONS"] == 50.0                 # existing fields filled for 2026Q3


def test_headers_in_template_order():
    assert b.HEADERS[:9] == ["FRS ID", "CERTIFICATE NUMBER", "INSTITUTION NAME", "STATE", "PARENT NUMBER",
                             "HOLD TYPE", "YEAR PERIOD", "FORM", "CBLR"]
    assert len(b.HEADERS) == 158 and b.HEADERS[-2:] == ["UST10Y", "UST30Y"]
