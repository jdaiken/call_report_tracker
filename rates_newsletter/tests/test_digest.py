import html as htmllib
from datetime import date, timedelta

from rates_digest import analysis
from rates_digest.email_report import Bundle
from rates_digest.extras import FailureResult, fomc_events, parse_failures, parse_release_dates, parse_stablecoins
from rates_digest.fetch import Observations, parse_fred_csv
from rates_digest.newsletter import (AREAS, ITEMS, build_report, current_zone, insight_line, render_html, render_text,
                                     stablecoin_reading)
from rates_digest.sample_data import sample_bundle, sample_observations
from rates_digest.series import SERIES_BY_ID, SPREADS

TODAY = date(2026, 10, 26)


def obs(sid, pts):
    return Observations(sid, pts)


def test_catalog_is_consistent():
    known = set(SERIES_BY_ID) | {sp.key for sp in SPREADS} | {"STABLECOINS"}
    for sp in SPREADS:
        assert sp.a in SERIES_BY_ID and sp.b in SERIES_BY_ID
    for a in AREAS:
        for key in a.items:
            assert key in ITEMS and key in known, key
        for ch in a.charts:
            for _, key in ch.lines:
                assert key in known, key
    for item in ITEMS.values():          # every item explains itself; zones are ordered, no gaps
        assert item.what and item.why
        for x, y in zip(item.zones, item.zones[1:]):
            assert x.hi == y.lo


def test_parse_fred_csv_handles_missing_and_both_headers():
    for header in ("DATE", "observation_date"):
        pts = parse_fred_csv(f"{header},DGS3MO\n2026-10-20,3.90\n2026-10-21,.\n2026-10-22,3.95\n")
        assert pts == [(date(2026, 10, 20), 3.90), (date(2026, 10, 22), 3.95)]


def test_millions_series_convert_even_without_units():
    r = analysis.build_reading(SERIES_BY_ID["WLCFLPCL"], obs("WLCFLPCL", [(TODAY - timedelta(days=7), 6000.0), (TODAY, 9169.0)]), TODAY)
    assert r.display_value == "$9 billion" and r.change_text("week") == "up $3 billion"


def test_quarterly_series_not_stale_between_releases():
    r = analysis.build_reading(SERIES_BY_ID["DRCRELEXFACBS"], obs("DRCRELEXFACBS", [(date(2026, 1, 1), 1.40), (date(2026, 4, 1), 1.53)]), TODAY)
    assert not r.stale and r.change_text("last") == "up 0.13 points"


def test_zones_and_insights():
    vix = analysis.build_reading(SERIES_BY_ID["VIXCLS"], obs("VIXCLS", [(TODAY - timedelta(days=1), 18.0), (TODAY, 32.0)]), TODAY)
    assert current_zone(ITEMS["VIXCLS"], vix).tone == "risk"
    b = Bundle(TODAY, {"VIXCLS": vix}, [])
    assert insight_line("VIXCLS", vix, b) == "32.0: panic."
    dep = analysis.build_reading(SERIES_BY_ID["DPSSCBW027SBOG"],
                                 obs("DPSSCBW027SBOG", [(TODAY - timedelta(days=365), 5700.0), (TODAY, 5650.0)]), TODAY)
    z = current_zone(ITEMS["DPSSCBW027SBOG"], dep)
    assert z.label == "Shrinking" and z.tone == "risk"


def test_rate_outlook_insight():
    data = {"DGS2": obs("DGS2", [(TODAY - timedelta(days=1), 3.4), (TODAY, 3.3)]),
            "EFFR": obs("EFFR", [(TODAY - timedelta(days=1), 3.88), (TODAY, 3.87)])}
    readings = analysis.build_all(data, TODAY)
    assert "markets expect cuts" in insight_line("DGS2", readings["DGS2"], Bundle(TODAY, readings, []))


def test_report_alerts_status_and_subject():
    b = sample_bundle(TODAY)
    rep = build_report(b)
    tones = {i.key: i.tone for a in rep.areas for i in a.items}
    assert set(tones) == {k for a in AREAS for k in a.items}
    n_alerts = sum(1 for t, _ in rep.alerts if t in ("risk", "watch"))
    assert rep.subject.startswith("IntraFi Risk Monitor") and (str(n_alerts) in rep.subject or "all normal" in rep.subject)
    assert any("The Fed cut rates" in x for _, x in rep.alerts)       # sample has a Fed cut 5 days ago


def test_email_and_text_render():
    b = sample_bundle(TODAY)
    rep = build_report(b)
    html, text = render_html(rep, b), render_text(rep)
    for key in ("Today's alerts", "Risk dashboard", "Insight:", "Why IntraFi cares:", "What it is:", "Ranges"):
        assert key in html or htmllib.escape(key, quote=True) in html, key
    for item in ITEMS.values():
        assert htmllib.escape(item.name, quote=True) in html and item.name in text
    for a in rep.areas:
        for c in a.charts:
            assert f'src="cid:{c.cid}"' in html and c.png[:4] == b"\x89PNG"
    assert "SAMPLE DATA" in html and "SAMPLE DATA" in text


def test_pdf_renders():
    from rates_digest.pdf import render_pdf
    rep = build_report(sample_bundle(TODAY))
    pdf = render_pdf(rep)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 50_000


def test_failed_series_does_not_break_report():
    data = sample_observations(TODAY)
    data["VIXCLS"] = Observations("VIXCLS", [], error="HTTP 500")
    readings = analysis.build_all(data, TODAY)
    b = Bundle(TODAY, readings, analysis.watch_flags(readings))
    rep = build_report(b)
    vix = next(i for a in rep.areas for i in a.items if i.key == "VIXCLS")
    assert not vix.loaded and "Did not load" in render_html(rep, b)


def test_bank_failure_alert():
    fails = parse_failures({"data": [
        {"data": {"NAME": "Old Bank", "CITYST": "X, TX", "FAILDATE": "1/10/2025", "QBFDEP": 50000}},
        {"data": {"NAME": "New Bank", "CITYST": "Y, OH", "FAILDATE": "10/23/2026", "QBFDEP": 1200000}}]})
    assert [f.name for f in fails] == ["New Bank", "Old Bank"]
    rep = build_report(Bundle(TODAY, {}, [], failures=FailureResult(fails)))
    assert rep.alerts[0][0] == "risk" and "New Bank (Y, OH)" in rep.alerts[0][1] and "$1.2 billion" in rep.alerts[0][1]
    assert "Bank failures in 2026: 1" in rep.failures_line


def test_stablecoins():
    pts = parse_stablecoins([{"date": "1761264000", "totalCirculatingUSD": {"peggedUSD": 3.1e11}},
                             {"date": 1729728000, "totalCirculatingUSD": {"peggedUSD": 1.7e11}}, {"bad": 1}])
    r = stablecoin_reading(pts, pts[-1][0])
    assert r.display_value == "$310 billion"
    assert current_zone(ITEMS["STABLECOINS"], r).label.startswith("Over 75%")
    assert not stablecoin_reading([], TODAY, "HTTP 500").ok


def test_release_calendar_and_fomc():
    events = parse_release_dates({"release_dates": [
        {"release_name": "Consumer Price Index", "date": "2026-10-27"},
        {"release_name": "Some Obscure Survey", "date": "2026-10-27"}]})
    assert [e.label for e in events] == ["Inflation report (CPI)"]
    assert [e.when for e in fomc_events(date(2026, 10, 26), date(2026, 11, 2))] == [date(2026, 10, 28)]


def test_fetch_all_stops_early_when_fred_is_down(monkeypatch):
    from rates_digest import fetch
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = []

    def failing(sid):
        calls.append(sid)
        return Observations(sid, [], error="connection refused")
    assert len(fetch.fetch_all([f"S{i}" for i in range(10)], TODAY, fetcher=failing)) == 10 and len(calls) == 3


def test_attachment_is_added(monkeypatch):
    from rates_digest import email_report
    sent = {}

    class FakeSMTP:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["msg"] = msg
    monkeypatch.setattr(email_report.smtplib, "SMTP_SSL", FakeSMTP)
    email_report.send_email(email_report.EmailConfig("a@x.com", "pw", "a@x.com"), "s", "t", "<p>h</p>",
                            {"chart1": b"\x89PNGfake"}, [("report.pdf", b"%PDF-1.4 fake", "pdf")])
    names = [p.get_filename() for p in sent["msg"].walk() if p.get_filename()]
    assert "report.pdf" in names


def test_run_sample_writes_email_and_pdf(tmp_path):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("rates_run", pathlib.Path(__file__).parents[1] / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    assert run.main(["--sample", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "newsletter.html").exists() and list(tmp_path.glob("IntraFi-Risk-Monitor-*.pdf"))


def test_every_measure_links_to_its_source():
    from rates_digest.newsletter import sources_for
    for key in ITEMS:
        srcs = sources_for(key)
        assert srcs and all(u.startswith("https://") for _, u in srcs), key
    assert sources_for("sofr_vs_iorb")[0][1] == "https://fred.stlouisfed.org/series/SOFR"
    b = sample_bundle(TODAY)
    rep = build_report(b)
    html = render_html(rep, b)
    assert "Check the data:" in html and "https://fred.stlouisfed.org/series/DGS10" in html
    assert "https://defillama.com/stablecoins" in html and "https://www.fdic.gov/bank-failures/failed-bank-list" in html
    assert "https://fred.stlouisfed.org/series/" in render_text(rep)
    from rates_digest.pdf import render_pdf
    assert b"fred.stlouisfed.org/series/DGS10" in render_pdf(rep)
