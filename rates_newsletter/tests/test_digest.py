from datetime import date, timedelta

from rates_digest import analysis
import html as htmllib

from rates_digest.email_report import Bundle, render_daily_html, render_daily_text, sparkline
from rates_digest.fetch import Observations, parse_fred_csv, parse_rss
from rates_digest.sample_data import sample_bundle, sample_headlines, sample_observations
from rates_digest.series import SERIES, SERIES_BY_ID, SPREADS

TODAY = date(2026, 10, 26)


def test_series_ids_unique_and_spreads_reference_known_series():
    ids = [s.id for s in SERIES]
    assert len(ids) == len(set(ids))
    for sp in SPREADS:
        assert sp.a in SERIES_BY_ID and sp.b in SERIES_BY_ID


def test_parse_fred_csv_handles_missing_and_both_headers():
    for header in ("DATE", "observation_date"):
        pts = parse_fred_csv(f"{header},DGS3MO\n2026-10-20,3.90\n2026-10-21,.\n2026-10-22,3.95\n")
        assert pts == [(date(2026, 10, 20), 3.90), (date(2026, 10, 22), 3.95)]


def test_rate_changes_in_points_and_plain_summary():
    pts = [(TODAY - timedelta(days=400), 5.0), (TODAY - timedelta(days=30), 4.5), (TODAY - timedelta(days=1), 4.25),
           (TODAY, 4.25)]
    r = analysis.build_reading(SERIES_BY_ID["DGS3MO"], Observations("DGS3MO", pts), TODAY)
    assert r.change_text("last") == "unchanged"
    assert r.change_text("month") == "down 0.25 points"
    assert r.change_text("year") == "down 0.75 points"
    assert "lowest level of the past year" in r.summary()


def test_level_changes_are_percent_and_millions_convert_to_billions():
    pts = [(TODAY - timedelta(days=365), 3_000_000.0), (TODAY, 3_300_000.0)]
    r = analysis.build_reading(SERIES_BY_ID["WRESBAL"], Observations("WRESBAL", pts, "Millions of U.S. Dollars"), TODAY)
    assert abs(r.value - 3300.0) < 1e-6
    assert r.display_value == "$3.30 trillion"
    assert r.change_text("year") == "up 10.0%"


def test_cpi_shown_as_year_over_year_inflation():
    pts = [(date(2025, 9, 1), 300.0), (date(2026, 9, 1), 309.0)]
    r = analysis.build_reading(SERIES_BY_ID["CPIAUCSL"], Observations("CPIAUCSL", pts), TODAY)
    assert abs(r.value - 3.0) < 1e-9
    assert r.display_value == "3.00%"


def test_monthly_series_has_no_fake_week_change():
    pts = [(date(2026, 8, 1), 0.60), (date(2026, 9, 1), 0.62), (date(2026, 10, 1), 0.63)]
    r = analysis.build_reading(SERIES_BY_ID["MMNDR"], Observations("MMNDR", pts), TODAY)
    assert r.changes.get("week") is None
    assert r.change_text("month") == "up 0.01 points"


def test_spread_uses_latest_monthly_value_for_daily_series():
    data = {
        "DGS3MO": Observations("DGS3MO", [(date(2026, 10, 22), 4.0), (date(2026, 10, 23), 4.1)]),
        "MMNDR": Observations("MMNDR", [(date(2026, 10, 1), 0.6)]),
    }
    sp = next(s for s in SPREADS if s.key == "bill_vs_mm")
    r = analysis.build_spread(sp, data, TODAY)
    assert abs(r.value - 3.5) < 1e-9


def test_stale_series_is_flagged_in_summary():
    pts = [(date(2026, 8, 1), 4.0), (date(2026, 8, 4), 4.1)]
    r = analysis.build_reading(SERIES_BY_ID["DGS2"], Observations("DGS2", pts), TODAY)
    assert r.stale and "Latest available data is from Aug 4, 2026" in r.summary()


def test_failed_series_does_not_break_digest():
    data = sample_observations(TODAY)
    data["VIXCLS"] = Observations("VIXCLS", [], error="HTTP 500")
    readings = analysis.build_all(data, TODAY)
    assert not readings["VIXCLS"].ok
    b = Bundle(TODAY, readings, analysis.watch_flags(readings))
    html = render_daily_html(b)
    assert "Did not load today" in html and "VIX" in html


def test_watch_flags_fire_on_inversion_and_deposit_shift():
    def obs(sid, month_ago, now):
        return Observations(sid, [(TODAY - timedelta(days=30), month_ago), (TODAY, now)])
    data = {
        "DGS10": obs("DGS10", 4.0, 3.8), "DGS2": obs("DGS2", 4.0, 4.0),
        "DPSSCBW027SBOG": obs("DPSSCBW027SBOG", 5700, 5650), "DPSLCBW027SBOG": obs("DPSLCBW027SBOG", 10000, 10100),
    }
    readings = analysis.build_all(data, TODAY)
    texts = " ".join(f.text for f in analysis.watch_flags(readings))
    assert "inverted" in texts
    assert "small banks while rising at large banks" in texts


def test_fed_cut_flag_from_sample_data():
    readings = analysis.build_all(sample_observations(TODAY), TODAY)
    texts = " ".join(f.text for f in analysis.watch_flags(readings))
    assert "The Fed cut rates this week by 0.25 points" in texts


def test_daily_render_with_sample_data():
    b = sample_bundle(TODAY)
    html = render_daily_html(b)
    text = render_daily_text(b)
    for s in SERIES:                       # every rate still appears, one line each
        assert htmllib.escape(s.name, quote=True) in html
        assert s.name in text
    assert "Today in 30 seconds" in html and "Key numbers" in html
    assert "SAMPLE DATA" in html and "SAMPLE DATA" in text
    # Definitions moved to the look book: the daily email stays short.
    assert SERIES_BY_ID["IORB"].what_it_is not in html


def test_rss_parsing_filters_by_date():
    xml = """<rss><channel>
      <item><title>New</title><link>https://x/1</link><pubDate>Fri, 23 Oct 2026 14:00:00 GMT</pubDate></item>
      <item><title>Old</title><link>https://x/2</link><pubDate>Mon, 05 Oct 2026 14:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_rss(xml, "Monetary policy", since=date(2026, 10, 19))
    assert [h.title for h in items] == ["New"]


def test_sparkline_shape():
    hist = [(TODAY - timedelta(days=i), float(i)) for i in range(100, 0, -1)]
    s = sparkline(hist)
    assert s and s[0] == "█" and s[-1] == "▁"


def test_fetch_all_stops_early_when_fred_is_down(monkeypatch):
    from rates_digest import fetch
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = []

    def failing(sid):
        calls.append(sid)
        return Observations(sid, [], error="connection refused")

    out = fetch.fetch_all([f"S{i}" for i in range(10)], TODAY, fetcher=failing)
    assert len(calls) == 3
    assert all(o.error for o in out.values()) and len(out) == 10


def test_market_snapshot_line():
    from rates_digest.email_report import market_snapshot
    def obs(sid, prev, now):
        return Observations(sid, [(date(2026, 10, 22), prev), (date(2026, 10, 23), now)])
    data = {"SP500": obs("SP500", 6500, 6435), "DJIA": obs("DJIA", 45000, 45000),
            "NASDAQCOM": obs("NASDAQCOM", 21500, 21242), "DGS10": obs("DGS10", 4.15, 4.12)}
    readings = analysis.build_all(data, date(2026, 10, 24))
    line = market_snapshot(readings)
    assert line == ("Markets on Friday: S&P 500 down 1.0%, Dow flat, Nasdaq down 1.2%, "
                    "10-year Treasury 4.12% (down 0.03 points).")


# ------------------------------------------------------------- new pieces
def test_millions_series_convert_even_without_units():
    pts = [(TODAY - timedelta(days=7), 6000.0), (TODAY, 9169.0)]   # FRED reports WLCFLPCL in millions
    r = analysis.build_reading(SERIES_BY_ID["WLCFLPCL"], Observations("WLCFLPCL", pts), TODAY)
    assert r.display_value == "$9 billion"
    assert r.change_text("week") == "up $3 billion"


def test_compact_change_forms():
    pts = [(TODAY - timedelta(days=30), 4.0), (TODAY - timedelta(days=1), 3.9), (TODAY, 3.77)]
    r = analysis.build_reading(SERIES_BY_ID["DGS3MO"], Observations("DGS3MO", pts), TODAY)
    assert r.compact_change("last") == "−0.13 pts"
    lv = analysis.build_reading(SERIES_BY_ID["SP500"], Observations("SP500", [(TODAY - timedelta(days=1), 100.0), (TODAY, 101.0)]), TODAY)
    assert lv.compact_change("last") == "+1.0%"


def test_quarterly_series_not_stale_between_releases():
    pts = [(date(2026, 1, 1), 1.40), (date(2026, 4, 1), 1.53)]
    r = analysis.build_reading(SERIES_BY_ID["DRCRELEXFACBS"], Observations("DRCRELEXFACBS", pts), TODAY)
    assert not r.stale
    assert r.change_text("last") == "up 0.13 points"


def test_discount_window_jump_flag():
    data = {"WLCFLPCL": Observations("WLCFLPCL", [(TODAY - timedelta(days=7), 5000.0), (TODAY, 14000.0)])}
    readings = analysis.build_all(data, TODAY)
    texts = " ".join(f.text for f in analysis.watch_flags(readings))
    assert "discount window jumped $9 billion" in texts


def test_parse_fdic_failures_and_flag():
    from rates_digest.email_report import extra_flags
    from rates_digest.extras import FailureResult, parse_failures
    payload = {"data": [
        {"data": {"NAME": "Old Bank", "CITYST": "X, TX", "FAILDATE": "1/10/2025", "QBFDEP": 50000}},
        {"data": {"NAME": "New Bank", "CITYST": "Y, OH", "FAILDATE": "10/23/2026", "QBFDEP": 1200000}},
    ]}
    fails = parse_failures(payload)
    assert [f.name for f in fails] == ["New Bank", "Old Bank"]
    b = Bundle(TODAY, {}, [], failures=FailureResult(fails))
    texts = " ".join(f.text for f in extra_flags(b))
    assert "The FDIC closed New Bank (Y, OH) on Oct 23, with $1.2 billion in deposits" in texts
    assert "Old Bank" not in texts


def test_parse_stablecoins():
    from rates_digest.extras import parse_stablecoins
    pts = parse_stablecoins([{"date": "1761264000", "totalCirculatingUSD": {"peggedUSD": 3.1e11}},
                             {"date": 1761177600, "totalCirculatingUSD": {"peggedUSD": 3.0e11}},
                             {"bad": 1}])
    assert len(pts) == 2 and pts[0][0] < pts[1][0] and abs(pts[1][1] - 310.0) < 1e-9


def test_release_calendar_matches_key_releases_and_fomc():
    from rates_digest.extras import fomc_events, parse_release_dates
    events = parse_release_dates({"release_dates": [
        {"release_name": "Consumer Price Index", "date": "2026-10-27"},
        {"release_name": "Some Obscure Survey", "date": "2026-10-27"},
        {"release_name": "Employment Situation", "date": "2026-10-30"}]})
    assert [e.label for e in events] == ["Inflation report (CPI)", "Jobs report"]
    fomc = fomc_events(date(2026, 10, 26), date(2026, 11, 2))
    assert len(fomc) == 1 and fomc[0].when == date(2026, 10, 28)


def test_regional_bank_stats_and_stress_flag():
    from rates_digest.email_report import extra_flags, regional_line
    from rates_digest.extras import BankQuote, RegionalBanks, _pct
    assert _pct(-0.0123) == -1.23 and _pct("-1.5%") == -1.5
    quotes = [BankQuote(f"B{i}", f"Bank {i}", 1e9, -4.0) for i in range(30)]
    rb = RegionalBanks(quotes)
    assert abs(rb.cap_weighted_change + 4.0) < 1e-9 and rb.share_down == 100
    assert "100% of 30 banks fell" in regional_line(rb)
    b = Bundle(TODAY, {}, [], regional=rb)
    assert any("Regional bank stocks fell 4.0%" in f.text for f in extra_flags(b))


def test_regional_banks_failure_is_safe(monkeypatch):
    import builtins
    from rates_digest import extras
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name.startswith("finvizfinance"):
            raise ImportError("blocked")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    rb = extras.fetch_regional_banks()
    assert not rb.ok and "blocked" in rb.error


def test_lookbook_renders_charts_and_definitions():
    from rates_digest.lookbook import render_lookbook
    b = sample_bundle(TODAY, weekly=True)
    html, text, images, subject = render_lookbook(b)
    assert len(images) == 12 and all(png[:4] == b"\x89PNG" for png in images.values())
    for cid in images:
        assert f'src="cid:{cid}"' in html
    assert "The week in one page" in html and "Reference: what every rate means" in html
    assert htmllib.escape(SERIES_BY_ID["IORB"].what_it_is, quote=True) in html
    assert subject.startswith("Weekly Look Book")


def test_auto_edition_picks_weekly_on_monday(monkeypatch, tmp_path):
    # Load this folder's run.py by path: the FFIEC monitor has its own run.py.
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("rates_run", pathlib.Path(__file__).parents[1] / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    from rates_digest import fetch
    monkeypatch.setattr(fetch, "today_eastern", lambda: date(2026, 10, 26))   # a Monday
    assert run.main(["--sample", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "lookbook.html").exists() and not (tmp_path / "digest.html").exists()
    monkeypatch.setattr(fetch, "today_eastern", lambda: date(2026, 10, 27))   # Tuesday
    assert run.main(["--sample", "--out-dir", str(tmp_path)]) == 0
    assert (tmp_path / "digest.html").exists()
