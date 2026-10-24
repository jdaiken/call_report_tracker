"""Rates & markets newsletter: a short weekday email and a Monday look book.

  python run.py                       auto: Monday -> weekly look book, Tue-Fri -> daily email
  python run.py --edition daily       force the daily email
  python run.py --edition weekly      force the look book
  python run.py --dry-run             build it from live data, write HTML/text/PNGs, don't email
  python run.py --sample              made-up sample data (no network), write preview files
  python run.py --check-series        test that every data source loads, then exit
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from rates_digest import analysis, extras, fetch
from rates_digest.email_report import (Bundle, EmailConfig, daily_subject, render_daily_html, render_daily_text,
                                       send_email)
from rates_digest.series import SERIES, SERIES_BY_ID, SPREADS

log = logging.getLogger("rates_digest")


def collect(today, weekly: bool) -> Bundle:
    ids = sorted({s.id for s in SERIES} | {sp.a for sp in SPREADS} | {sp.b for sp in SPREADS})
    long_ids = [i for i in ids if SERIES_BY_ID.get(i) and SERIES_BY_ID[i].frequency == "quarterly"]
    data = fetch.fetch_all([i for i in ids if i not in long_ids], start=today - timedelta(days=800))
    data.update(fetch.fetch_all(long_ids, start=today - timedelta(days=2000)))
    headlines, feed_errors = fetch.fetch_headlines(since=today - timedelta(days=7))
    readings = analysis.build_all(data, today)
    return Bundle(
        today=today, readings=readings, flags=analysis.watch_flags(readings),
        headlines=headlines, feed_errors=feed_errors,
        failures=extras.fetch_failures(),
        stablecoins=extras.fetch_stablecoins() if weekly else extras.StablecoinResult(),
        calendar=extras.fetch_calendar(today, days=8),
        regional=extras.fetch_regional_banks(with_performance=weekly),
        used_api=bool(os.environ.get("FRED_API_KEY", "").strip()),
    )


def check_sources(b: Bundle) -> int:
    bad = 0
    for key, r in sorted(b.readings.items()):
        if r.ok:
            print(f"OK    {key:16} latest {r.as_of} = {r.display_value}{'  (STALE)' if r.stale else ''}")
        else:
            bad += 1
            print(f"FAIL  {key:16} {r.error}")
    extra_checks = [
        ("FDIC bank failures", b.failures.error, f"{len(b.failures.failures)} records, newest "
         f"{b.failures.failures[0].fail_date if b.failures.failures else 'n/a'}"),
        ("Stablecoins", b.stablecoins.error, f"{len(b.stablecoins.points)} days, latest "
         f"${b.stablecoins.points[-1][1]:,.0f}B" if b.stablecoins.points else ""),
        ("Release calendar", b.calendar.error, f"{len(b.calendar.events)} events in the next 8 days"),
        ("Regional banks (Finviz)", b.regional.error, f"{len(b.regional.quotes)} banks"),
        ("Fed headlines", ", ".join(b.feed_errors) or None, f"{len(b.headlines)} in the past week"),
    ]
    for name, err, ok_text in extra_checks:
        print(f"{'FAIL' if err else 'OK  '}  {name:24} {err or ok_text}")
        bad += 1 if err else 0
    print(f"\n{bad} source(s) failed.")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--edition", choices=["auto", "daily", "weekly"], default="auto")
    ap.add_argument("--dry-run", action="store_true", help="write files instead of emailing")
    ap.add_argument("--sample", action="store_true", help="use made-up sample data (implies --dry-run)")
    ap.add_argument("--check-series", action="store_true", help="report which sources load, then exit")
    ap.add_argument("--out-dir", default=".", help="where --dry-run writes its files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    today = fetch.today_eastern()
    edition = args.edition
    if edition == "auto":
        edition = "weekly" if today.weekday() == 0 else "daily"
    weekly = edition == "weekly" or args.check_series

    if args.sample:
        from rates_digest.sample_data import sample_bundle
        b = sample_bundle(today, weekly=weekly)
    else:
        b = collect(today, weekly)

    if args.check_series:
        return check_sources(b)

    loaded = sum(1 for r in b.readings.values() if r.ok)
    if loaded < len(b.readings) / 2:
        log.error("Only %d of %d readings loaded -- not sending a mostly empty email.", loaded, len(b.readings))
        return 1

    images: dict[str, bytes] = {}
    if edition == "weekly":
        from rates_digest.lookbook import render_lookbook
        html_body, text_body, images, subject = render_lookbook(b)
    else:
        html_body, text_body, subject = render_daily_html(b), render_daily_text(b), daily_subject(b)

    if args.dry_run or args.sample:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = "lookbook" if edition == "weekly" else "digest"
        preview = html_body
        for cid in images:   # point the saved HTML at the saved PNGs so it opens in a browser
            (out / f"{cid}.png").write_bytes(images[cid])
            preview = preview.replace(f'"cid:{cid}"', f'"{cid}.png"')
        (out / f"{stem}.html").write_text(preview, encoding="utf-8")
        (out / f"{stem}.txt").write_text(text_body, encoding="utf-8")
        print(f"Subject: {subject}\nWrote {out / (stem + '.html')} (+ {len(images)} chart images)")
        return 0

    send_email(EmailConfig.from_env(), subject, text_body, html_body, images)
    log.info("Sent %s edition: %s", edition, subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
