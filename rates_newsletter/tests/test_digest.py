import re
from datetime import date, timedelta

from rates_digest import analysis
from rates_digest.email_report import Bundle
from rates_digest.extras import FailureResult, fomc_events, parse_failures, parse_release_dates, parse_stablecoins
from rates_digest.fetch import Observations, parse_fred_csv
from rates_digest.monitor import CHART_PAGES, LAST_PAGE_PANELS, RATE_GROUPS, RATES_PANELS, SUMMARY_GROUPS, build_report, render_html, render_text, signal
from rates_digest.newsletter import ITEMS, current_zone, sources_for, stablecoin_reading
from rates_digest.pdf import render_pdf
from rates_digest.sample_data import sample_bundle, sample_observations
from rates_digest.series import SERIES_BY_ID, SPREADS

TODAY = date(2026, 10, 26)


def obs(sid, pts):
    return Observations(sid, pts)


def pdf_pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type /Page[^s]", pdf))


def test_layout_is_consistent():
    known = set(SERIES_BY_ID) | {sp.key for sp in SPREADS} | {"STABLECOINS"}
    summary_keys = [k for _, keys in SUMMARY_GROUPS for k in keys]
    assert set(summary_keys) == set(ITEMS)                       # every measure is on the summary page
    assert len(CHART_PAGES) == 2 and all(len(p.panels) == 4 for p in CHART_PAGES)
    for spec in [s for p in CHART_PAGES for s in p.panels] + RATES_PANELS + LAST_PAGE_PANELS:
        assert spec.key in ITEMS
        for _, key in spec.lines:
            assert key in known, key
    for item in ITEMS.values():
        for x, y in zip(item.zones, item.zones[1:]):
            assert x.hi == y.lo


def test_pdf_is_exactly_five_pages_and_unbranded():
    b = sample_bundle(TODAY)
    rep = build_report(b)
    assert pdf_pages(render_pdf(rep)) == 5
    for text in (render_html(rep, b), render_text(rep), rep.subject):
        assert "IntraFi" not in text and "Executive" not in text


def test_five_pages_even_on_a_bad_day():
    """Lots of alerts and bank failures must not push the PDF past five pages."""
    data = sample_observations(TODAY)
    data["VIXCLS"] = Observations("VIXCLS", [(TODAY - timedelta(days=i), 40.0 + i % 3) for i in range(400, 0, -1)])
    readings = analysis.build_all(data, TODAY)
    fails = [parse_failures({"data": [{"data": {"NAME": f"Bank {i}", "CITYST": "X, TX",
                                                "FAILDATE": f"{TODAY - timedelta(days=i):%m/%d/%Y}", "QBFDEP": 900000}}]})[0]
             for i in range(1, 9)]
    b = Bundle(TODAY, readings, analysis.watch_flags(readings), failures=FailureResult(fails))
    assert pdf_pages(render_pdf(build_report(b))) == 5


def test_no_instructions_in_report():
    b = sample_bundle(TODAY)
    html = render_html(build_report(b), b)
    for phrase in ("What it is", "Why", "How to read", "rules of thumb", "Click any"):
        assert phrase not in html


def test_summary_counts_and_signals():
    rep = build_report(sample_bundle(TODAY))
    rows = [r for _, rs in rep.groups for r in rs]
    assert sum(rep.counts.values()) == sum(1 for r in rows if r.tone in ("risk", "watch", "ok"))
    assert rep.signals and rep.signals[0][0] in ("risk", "watch")
    assert any("Fed cut 0.25" in x for _, x in rep.signals)      # sample has a Fed cut 5 days ago
    assert rep.subject.startswith("Rates & Deposit Risk Monitor")


def test_signals_are_short_and_specific():
    b = sample_bundle(TODAY)
    for key in ITEMS:
        r = b.readings[key]
        s = signal(key, r, b)
        assert s and len(s) <= 90, (key, s)
    vix = analysis.build_reading(SERIES_BY_ID["VIXCLS"], obs("VIXCLS", [(TODAY - timedelta(days=1), 18.0), (TODAY, 32.0)]), TODAY)
    assert current_zone(ITEMS["VIXCLS"], vix).tone == "risk"
    assert signal("VIXCLS", vix, Bundle(TODAY, {"VIXCLS": vix}, [])).startswith("Panic")


def test_every_reading_links_to_its_source():
    for key in ITEMS:
        assert all(u.startswith("https://") for _, u in sources_for(key))
    b = sample_bundle(TODAY)
    rep = build_report(b)
    html = render_html(rep, b)
    assert "https://fred.stlouisfed.org/series/DGS10" in html and "https://defillama.com/stablecoins" in html
    assert "https://www.fdic.gov/bank-failures/failed-bank-list" in html
    assert b"fred.stlouisfed.org/series/DGS10" in render_pdf(rep)


def test_charts_embedded():
    b = sample_bundle(TODAY)
    rep = build_report(b)
    html = render_html(rep, b)
    panels = rep.all_panels
    assert len(panels) == 13
    for p in panels:
        assert p.png and p.png[:4] == b"\x89PNG" and f'src="cid:{p.cid}"' in html


def test_failed_series_does_not_break_report():
    data = sample_observations(TODAY)
    data["VIXCLS"] = Observations("VIXCLS", [], error="HTTP 500")
    readings = analysis.build_all(data, TODAY)
    b = Bundle(TODAY, readings, analysis.watch_flags(readings))
    rep = build_report(b)
    assert "Market fear (VIX)" in rep.missing
    assert pdf_pages(render_pdf(rep)) == 5


def test_bank_failure_signal():
    fails = parse_failures({"data": [
        {"data": {"NAME": "Old Bank", "CITYST": "X, TX", "FAILDATE": "1/10/2025", "QBFDEP": 50000}},
        {"data": {"NAME": "New Bank", "CITYST": "Y, OH", "FAILDATE": "10/23/2026", "QBFDEP": 1200000}}]})
    rep = build_report(Bundle(TODAY, {}, [], failures=FailureResult(fails)))
    assert rep.signals[0] == ("risk", "Bank failure: New Bank (Y, OH), Oct 23, $1.2 billion deposits")
    assert rep.failures_ytd == 1


def test_data_parsers():
    for header in ("DATE", "observation_date"):
        assert parse_fred_csv(f"{header},X\n2026-10-20,3.90\n2026-10-21,.\n") == [(date(2026, 10, 20), 3.90)]
    pts = parse_stablecoins([{"date": "1761264000", "totalCirculatingUSD": {"peggedUSD": 3.1e11}},
                             {"date": 1729728000, "totalCirculatingUSD": {"peggedUSD": 1.7e11}}, {"bad": 1}])
    assert stablecoin_reading(pts, pts[-1][0]).display_value == "$310 billion"
    assert [e.label for e in parse_release_dates({"release_dates": [
        {"release_name": "Consumer Price Index", "date": "2026-10-27"}]})] == ["Inflation report (CPI)"]
    assert [e.when for e in fomc_events(date(2026, 10, 26), date(2026, 11, 2))] == [date(2026, 10, 28)]
    r = analysis.build_reading(SERIES_BY_ID["WLCFLPCL"], obs("WLCFLPCL", [(TODAY - timedelta(days=7), 6000.0), (TODAY, 9169.0)]), TODAY)
    assert r.display_value == "$9 billion"
    q = analysis.build_reading(SERIES_BY_ID["DRCRELEXFACBS"], obs("DRCRELEXFACBS", [(date(2026, 1, 1), 1.4), (date(2026, 4, 1), 1.53)]), TODAY)
    assert not q.stale


def test_fetch_all_stops_early_when_fred_is_down(monkeypatch):
    from rates_digest import fetch
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    calls = []

    def failing(sid):
        calls.append(sid)
        return Observations(sid, [], error="connection refused")
    assert len(fetch.fetch_all([f"S{i}" for i in range(10)], TODAY, fetcher=failing)) == 10 and len(calls) == 3


def test_email_attaches_pdf(monkeypatch):
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
    assert "report.pdf" in [p.get_filename() for p in sent["msg"].walk() if p.get_filename()]


def test_run_sample_writes_email_and_pdf(tmp_path):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("rates_run", pathlib.Path(__file__).parents[1] / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    assert run.main(["--sample", "--out-dir", str(tmp_path)]) == 0
    pdfs = list(tmp_path.glob("Rates-Deposit-Risk-Monitor-*.pdf"))
    assert (tmp_path / "newsletter.html").exists() and pdfs and pdf_pages(pdfs[0].read_bytes()) == 5


def test_rates_table_has_every_daily_rate():
    b = sample_bundle(TODAY)
    rep = build_report(b)
    keys = [k for _, ks in RATE_GROUPS for k, _ in ks]
    assert len(keys) == 17 and all(k in SERIES_BY_ID for k in keys)
    html = render_html(rep, b)
    for _, rows in rep.rates:
        for r in rows:
            assert r.latest != "n/a", r.name
            assert r.name in html and r.url in html
    pdf = render_pdf(rep)
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) == 5


def test_published_failed_bank_list_fills_api_lag():
    from rates_digest.extras import merge_failures, parse_failed_bank_list, parse_failures
    csv_text = ('﻿Bank Name\xa0,City\xa0,State\xa0,Cert\xa0,Acquiring Institution\xa0,Closing Date\xa0,Fund\n'
                'Nano Banc,Irvine,CA,57263,Sunwest Bank,25-Sep-26,10550\n'
                'Small Business Bank,Lenexa,KS,58000,Some Bank,17-Jul-26,10549\n')
    listed = parse_failed_bank_list(csv_text)
    assert len(parse_failed_bank_list(csv_text.replace('\xa0', '\ufffd'))) == 2
    assert [f.name for f in listed] == ["Nano Banc", "Small Business Bank"]
    api = parse_failures({"data": [{"data": {"NAME": "SMALL BUSINESS BANK", "CITYST": "LENEXA, KS",
                                             "FAILDATE": "7/17/2026", "QBFDEP": "100000"}}]})
    merged = merge_failures(api, listed)
    assert len(merged) == 2 and merged[0].name == "Nano Banc" and merged[1].deposits_thousands == 100000


def test_every_chart_links_all_its_sources_in_pdf_and_email():
    from pypdf import PdfReader
    import io
    b = sample_bundle(TODAY)
    rep = build_report(b)
    html = render_html(rep, b)
    pdf = render_pdf(rep)
    uris = {a.get_object()["/A"]["/URI"] for pg in PdfReader(io.BytesIO(pdf)).pages
            for a in pg.get("/Annots", []) if "/A" in a.get_object()}
    for p in rep.all_panels:
        assert p.sources
        for _, u in p.sources:
            assert u in uris and u in html, (p.title, u)
    assert len(re.findall(rb"/Type /Page[^s]", pdf)) == 5
