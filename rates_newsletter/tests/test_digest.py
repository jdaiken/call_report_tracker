from datetime import date, timedelta

from rates_digest import analysis
from rates_digest.email_report import render_html, render_text, sparkline
from rates_digest.fetch import Observations, parse_fred_csv, parse_rss
from rates_digest.sample_data import sample_headlines, sample_observations
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
    html = render_html(TODAY, readings, analysis.watch_flags(readings), [], [], False)
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


def test_full_render_with_sample_data():
    readings = analysis.build_all(sample_observations(TODAY), TODAY)
    flags = analysis.watch_flags(readings)
    html = render_html(TODAY, readings, flags, sample_headlines(TODAY), [], False)
    text = render_text(TODAY, readings, flags, sample_headlines(TODAY))
    for s in SERIES:
        assert s.name.replace("&", "&amp;") in html
        assert s.name in text
    assert "Today in 30 seconds" in html


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
