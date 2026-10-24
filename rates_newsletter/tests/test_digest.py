import html as htmllib
from datetime import date, timedelta

from rates_digest import analysis
from rates_digest.email_report import Bundle
from rates_digest.extras import FailureResult, fomc_events, parse_failures, parse_release_dates
from rates_digest.fetch import Observations, parse_fred_csv
from rates_digest.newsletter import ITEMS, SECTIONS, current_zone, means_text, render
from rates_digest.sample_data import sample_bundle, sample_observations
from rates_digest.series import SERIES, SERIES_BY_ID, SPREADS

TODAY = date(2026, 10, 26)


def obs(sid, pts):
    return Observations(sid, pts)


def test_catalog_is_consistent():
    ids = [s.id for s in SERIES]
    assert len(ids) == len(set(ids))
    known = set(ids) | {sp.key for sp in SPREADS} | {"STABLECOINS"}   # stablecoins come from DeFiLlama
    for sp in SPREADS:
        assert sp.a in SERIES_BY_ID and sp.b in SERIES_BY_ID
    for sec in SECTIONS:
        for key in sec.items:
            assert key in known, key
        for ch in sec.charts:
            for _, key in ch.lines:
                assert key in known, key
    for item in ITEMS.values():          # zones are ordered and don't overlap
        for a, b in zip(item.zones, item.zones[1:]):
            assert a.hi == b.lo


def test_parse_fred_csv_handles_missing_and_both_headers():
    for header in ("DATE", "observation_date"):
        pts = parse_fred_csv(f"{header},DGS3MO\n2026-10-20,3.90\n2026-10-21,.\n2026-10-22,3.95\n")
        assert pts == [(date(2026, 10, 20), 3.90), (date(2026, 10, 22), 3.95)]


def test_rate_changes_and_past_year_range():
    pts = [(TODAY - timedelta(days=400), 5.0), (TODAY - timedelta(days=30), 4.5), (TODAY - timedelta(days=1), 4.25), (TODAY, 4.25)]
    r = analysis.build_reading(SERIES_BY_ID["DGS3MO"], obs("DGS3MO", pts), TODAY)
    assert r.change_text("month") == "down 0.25 points"
    assert r.compact_change("year") == "−0.75 pts"
    assert (r.low_1y, r.high_1y) == (4.25, 4.5)


def test_millions_series_convert_even_without_units():
    pts = [(TODAY - timedelta(days=7), 6000.0), (TODAY, 9169.0)]
    r = analysis.build_reading(SERIES_BY_ID["WLCFLPCL"], obs("WLCFLPCL", pts), TODAY)
    assert r.display_value == "$9 billion" and r.change_text("week") == "up $3 billion"


def test_cpi_shown_as_year_over_year_inflation():
    r = analysis.build_reading(SERIES_BY_ID["CPIAUCSL"], obs("CPIAUCSL", [(date(2025, 9, 1), 300.0), (date(2026, 9, 1), 309.0)]), TODAY)
    assert r.display_value == "3.00%"
    assert current_zone(ITEMS["CPIAUCSL"], r).label.startswith("3% to 5%")


def test_quarterly_series_not_stale_between_releases():
    r = analysis.build_reading(SERIES_BY_ID["DRCRELEXFACBS"], obs("DRCRELEXFACBS", [(date(2026, 1, 1), 1.40), (date(2026, 4, 1), 1.53)]), TODAY)
    assert not r.stale and r.change_text("last") == "up 0.13 points"


def test_zones_value_yoy_and_drawdown():
    vix = analysis.build_reading(SERIES_BY_ID["VIXCLS"], obs("VIXCLS", [(TODAY - timedelta(days=1), 18.0), (TODAY, 32.0)]), TODAY)
    assert current_zone(ITEMS["VIXCLS"], vix).tone == "risk"
    dep = analysis.build_reading(SERIES_BY_ID["DPSSCBW027SBOG"],
                                 obs("DPSSCBW027SBOG", [(TODAY - timedelta(days=365), 5700.0), (TODAY, 5650.0)]), TODAY)
    z = current_zone(ITEMS["DPSSCBW027SBOG"], dep)
    assert z.label == "Shrinking" and z.tone == "risk"
    sp = analysis.build_reading(SERIES_BY_ID["SP500"], obs("SP500", [(TODAY - timedelta(days=100), 6000.0), (TODAY, 5100.0)]), TODAY)
    assert "correction" in current_zone(ITEMS["SP500"], sp).label
    b = Bundle(TODAY, {"SP500": sp}, [])
    assert "15.0% below its past-year high" in means_text("SP500", sp, b)


def test_means_text_uses_context():
    data = {"DGS2": obs("DGS2", [(TODAY - timedelta(days=1), 3.4), (TODAY, 3.3)]),
            "EFFR": obs("EFFR", [(TODAY - timedelta(days=1), 3.88), (TODAY, 3.87)])}
    readings = analysis.build_all(data, TODAY)
    text = means_text("DGS2", readings["DGS2"], Bundle(TODAY, readings, []))
    assert "markets expect the Fed to cut" in text


def test_failed_series_does_not_break_newsletter():
    data = sample_observations(TODAY)
    data["VIXCLS"] = Observations("VIXCLS", [], error="HTTP 500")
    readings = analysis.build_all(data, TODAY)
    html, text, images, subject = render(Bundle(TODAY, readings, analysis.watch_flags(readings)))
    assert "Did not load this week" in html and "VIX" in html and images


def test_full_render_with_sample_data():
    b = sample_bundle(TODAY)
    html, text, images, subject = render(b)
    assert len(images) == sum(len(s.charts) for s in SECTIONS)
    assert all(png[:4] == b"\x89PNG" for png in images.values())
    for cid in images:
        assert f'src="cid:{cid}"' in html
    for key in ("What it is:", "What it means now:", "Risk to watch:", "Ranges to know", "The week in one page"):
        assert key in html
    for s in SERIES:
        if s.id not in ("DFEDTARL", "IORB"):   # shown via other items
            assert htmllib.escape(s.name, quote=True) in html, s.name
    assert subject.startswith("Weekly Rates & Markets") and "SAMPLE DATA" in html and "SAMPLE DATA" in text


def test_parse_fdic_failures_and_headline():
    payload = {"data": [
        {"data": {"NAME": "Old Bank", "CITYST": "X, TX", "FAILDATE": "1/10/2025", "QBFDEP": 50000}},
        {"data": {"NAME": "New Bank", "CITYST": "Y, OH", "FAILDATE": "10/23/2026", "QBFDEP": 1200000}},
    ]}
    fails = parse_failures(payload)
    assert [f.name for f in fails] == ["New Bank", "Old Bank"]
    from rates_digest.newsletter import headline_bullets
    texts = " ".join(t for _, t in headline_bullets(Bundle(TODAY, {}, [], failures=FailureResult(fails))))
    assert "The FDIC closed New Bank (Y, OH) on Oct 23, with $1.2 billion in deposits" in texts
    assert "Bank failures so far in 2026: 1" in texts


def test_release_calendar_and_fomc():
    events = parse_release_dates({"release_dates": [
        {"release_name": "Consumer Price Index", "date": "2026-10-27"},
        {"release_name": "Some Obscure Survey", "date": "2026-10-27"},
        {"release_name": "Employment Situation", "date": "2026-10-30"}]})
    assert [e.label for e in events] == ["Inflation report (CPI)", "Jobs report"]
    fomc = fomc_events(date(2026, 10, 26), date(2026, 11, 2))
    assert len(fomc) == 1 and fomc[0].when == date(2026, 10, 28)


def test_fetch_all_stops_early_when_fred_is_down(monkeypatch):
    from rates_digest import fetch
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = []

    def failing(sid):
        calls.append(sid)
        return Observations(sid, [], error="connection refused")
    out = fetch.fetch_all([f"S{i}" for i in range(10)], TODAY, fetcher=failing)
    assert len(calls) == 3 and len(out) == 10


def test_run_sample_writes_preview(tmp_path):
    # Load this folder's run.py by path: the FFIEC monitor has its own run.py.
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("rates_run", pathlib.Path(__file__).parents[1] / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    assert run.main(["--sample", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "newsletter.html").exists() and (tmp_path / "chart1.png").exists()


def test_stablecoins_parse_reading_and_section():
    from rates_digest.extras import parse_stablecoins
    from rates_digest.newsletter import stablecoin_reading
    pts = parse_stablecoins([{"date": "1761264000", "totalCirculatingUSD": {"peggedUSD": 3.1e11}},
                             {"date": 1729728000, "totalCirculatingUSD": {"peggedUSD": 1.7e11}}, {"bad": 1}])
    assert len(pts) == 2 and abs(pts[-1][1] - 310.0) < 1e-9
    r = stablecoin_reading(pts, pts[-1][0])
    assert r.display_value == "$310 billion"
    assert current_zone(ITEMS["STABLECOINS"], r).label.startswith("Above 75%")
    html, _, _, _ = render(sample_bundle(TODAY))
    assert "Stablecoins in circulation" in html and "GENIUS Act" in html
    assert not stablecoin_reading([], TODAY, "HTTP 500").ok
