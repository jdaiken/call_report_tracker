import io
import re
from datetime import date, datetime

from openpyxl import load_workbook

from ffiec_monitor import db, enforcement, network, network_render, ratios

Q3 = date(2026, 9, 30)


def _add(conn, i, r, assets=200_000, period=Q3):
    db.upsert_snapshot(conn, db.Snapshot(str(i), period, datetime.now(), r, datetime.now()))
    db.upsert_institution_meta(conn, db.InstitutionMeta(str(i), f"Bank {i}", "Springfield", "IL", str(9000 + i), assets, period))


def _r(rec, liab=100_000, unins=30_000, deps=90_000, lev=10.0):
    return {"_reciprocal_raw": rec, "_total_liabilities_raw": liab, "_uninsured_raw": unins, "_total_deposits_raw": deps,
            "tier1_leverage_ratio": lev, "total_rbc_ratio": 15.0}


def test_extraction_of_network_fields():
    fac = {"RCON2948": "1000000", "RCONJH83": "150000", "RCON5597": "300000", "RCON2200": "900000", "RCON2170": "1100000"}
    r = ratios.extract_ratios(fac, Q3)
    assert r["_reciprocal_raw"] == 150000 and r["_total_liabilities_raw"] == 1000000
    assert round(r["reciprocal_cap_used"], 2) == 75.0                      # cap = 20% of $1.0B = $200M
    assert round(r["uninsured_deposit_share"], 2) == 33.33
    big = ratios.extract_ratios({"RCON2948": "40000000", "RCONJH83": "4000000"}, Q3)
    assert round(big["reciprocal_cap_used"], 1) == 80.0                    # $5B cap binds, not 20% ($8B)


def test_network_report(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(":memory:")
    _add(conn, 1, _r(19_000))                           # 95% of a $20M cap
    _add(conn, 2, _r(25_000))                           # over the cap
    _add(conn, 3, _r(5_000, lev=4.5))                   # below well capitalized -> special cap
    _add(conn, 4, _r(0, unins=60_000))                  # 67% uninsured, no reciprocal -> prospect
    _add(conn, 5, _r(2_000))
    _add(conn, 6, _r(1_000), period=date(2025, 12, 31))  # stale quarter: excluded
    _add(conn, 7, {"tier1_leverage_ratio": 9.0})        # no network fields and no client: counted missing
    src = enforcement.Sources(actions=[enforcement.Action("FDIC", "Bank 5", "IL", "Consent Order", date(2025, 1, 1),
                                                          "u", "", "9005")], loaded={"FDIC": 1})
    net = network.collect(conn, None, date(2026, 10, 26), enforcement_sources=src,
                          prior_lookup=lambda rssd, p: _r(10_000))
    assert net.missing == 1 and len(net.banks) == 5
    s = net.stats()
    assert (s["users"], s["near_cap"], s["over_cap"], s["special"]) == (4, 1, 1, 2)
    assert [b.bank.name for b in net.near_cap()] == ["Bank 2", "Bank 1"]
    assert [b.bank.name for b in net.prospects()] == ["Bank 4"]
    assert {b.bank.name for b in net.special()} == {"Bank 3", "Bank 5"}
    assert net.near_cap()[1].prior_used == 50.0
    net = network.build(net)
    assert net.summary_by == "rules" and "over their reciprocal cap" in net.summary
    assert {"segments", "map", "top", "nearcap", "special", "cushion", "uninsured", "movers", "risk"} <= set(net.images)
    html = network_render.render_html(net)
    assert "Watch list" in html and "cid:net_risk" in html and "oncentration" not in html
    pdf = network_render.render_pdf(net)
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) == 7
    wb = load_workbook(io.BytesIO(network_render.build_workbook(net)))
    assert wb.sheetnames == ["Summary", "Banks", "5-quarter trend", "Top 20 holders", "Near cap", "Special cap", "Closest to special cap",
                             "High uninsured"]
    assert str(wb["Summary"]["B8"].value).startswith("=COUNTIF(Banks!")


def test_special_cap_estimate_and_closest(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(":memory:")
    _add(conn, 1, {**_r(5_000), "tier1_leverage_ratio": 4.5})                      # below well capitalized
    _add(conn, 2, {**_r(8_000), "tier1_leverage_ratio": 8.6, "total_rbc_ratio": None})   # CBLR bank under 9%
    _add(conn, 3, {**_r(8_000), "total_rbc_ratio": 10.4})                           # 0.4 pts above 10%
    _add(conn, 4, _r(8_000))
    net = network.collect(conn, None, date(2026, 10, 26), prior_lookup=lambda rssd, p: _r(3_000))
    spec = net.special()[0]
    assert spec.bank.name == "Bank 1" and spec.special_cap_est == 3_000 and spec.over_special == 2_000 and spec.est_basis == "4Q avg"
    close = net.closest_to_special()
    assert [b.bank.name for b in close][:2] == ["Bank 2", "Bank 3"]
    assert round(close[0].cushion, 2) == -0.4 and close[0].binding == "CBLR leverage (9%)"
    assert round(close[1].cushion, 2) == 0.4 and close[1].binding.startswith("total risk-based")
    assert [b.bank.name for b in net.top_exposure(2)] == ["Bank 2", "Bank 3"] or net.top_exposure(1)[0].reciprocal == 8_000


def test_large_banks_left_out(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(":memory:")
    _add(conn, 1, _r(19_000))
    _add(conn, 2, _r(4_000_000, liab=300_000_000), assets=60_000_000)             # $60B (Raymond James size): out
    db.upsert_institution_meta(conn, db.InstitutionMeta("3", "BANK OF AMERICA, NATIONAL ASSOCIATION", "Charlotte", "NC",
                                                        "3510", 90_000_000, Q3))  # GSIB name: out
    db.upsert_snapshot(conn, db.Snapshot("3", Q3, datetime.now(), _r(1_000), datetime.now()))
    net = network.collect(conn, None, date(2026, 10, 26))
    assert [b.bank.name for b in net.banks] == ["Bank 1"] and len(net.excluded) == 2
    assert not network.excluded_bank("FIRST STATE BANK", 500_000)


def test_history_cache_and_trend(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(":memory:")
    _add(conn, 1, _r(30_000))                                                       # 150% of a $20M cap

    class Client:
        calls = 0

        def retrieve_facsimile(self, rssd, period):
            Client.calls += 1
            return {"RCON2948": "100000", "RCONJH83": "20000"}                       # a year of 100% of cap
    look = network.make_history_lookup(conn, Client())
    net = network.collect(conn, None, date(2026, 10, 26), prior_lookup=look)
    b = net.banks[0]
    assert len(b.history) == 5 and Client.calls == 4 and b.gap_change() == 10_000
    assert [x.bank.name for x in net.trend()] == ["Bank 1"]
    network.collect(conn, None, date(2026, 10, 26), prior_lookup=look)
    assert Client.calls == 4                                                         # cached


def test_size_falls_back_when_meta_assets_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(":memory:")
    db.upsert_snapshot(conn, db.Snapshot("1", Q3, datetime.now(), _r(1_000, liab=40_000_000), datetime.now()))
    db.upsert_institution_meta(conn, db.InstitutionMeta("1", "RAYMOND JAMES BANK", "St Petersburg", "FL", "1", None, Q3))
    _add(conn, 2, _r(1_000))
    net = network.collect(conn, None, date(2026, 10, 26))
    assert net.excluded == ["RAYMOND JAMES BANK"] and [b.bank.name for b in net.banks] == ["Bank 2"]
