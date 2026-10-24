"""IntraFi Risk Monitor: one daily email (with the same report attached as a PDF).

  python run.py                 build from live data and email it
  python run.py --dry-run       build from live data, write HTML/text/PNGs, don't email
  python run.py --sample        made-up sample data (no network), write preview files
  python run.py --check-series  test that every data source loads, then exit
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from rates_digest import analysis, extras, fetch
from rates_digest.email_report import Bundle, EmailConfig, send_email
from rates_digest.newsletter import build_report, render_html, render_text, stablecoin_reading
from rates_digest.pdf import render_pdf
from rates_digest.series import SERIES, SERIES_BY_ID, SPREADS

log = logging.getLogger("rates_digest")


def collect(today) -> Bundle:
    ids = sorted({s.id for s in SERIES} | {sp.a for sp in SPREADS} | {sp.b for sp in SPREADS})
    long_ids = [i for i in ids if SERIES_BY_ID[i].frequency == "quarterly"]
    data = fetch.fetch_all([i for i in ids if i not in long_ids], start=today - timedelta(days=5 * 366))
    data.update(fetch.fetch_all(long_ids, start=today - timedelta(days=13 * 366)))
    readings = analysis.build_all(data, today)
    sc = extras.fetch_stablecoins()
    readings["STABLECOINS"] = stablecoin_reading(sc.points, today, sc.error)
    return Bundle(
        today=today, readings=readings, flags=analysis.watch_flags(readings),
        failures=extras.fetch_failures(),
        calendar=extras.fetch_calendar(today, days=7),
        used_api=bool(os.environ.get("FRED_API_KEY", "").strip()),
    )


def check_sources(b: Bundle) -> int:
    bad = []
    for key, r in sorted(b.readings.items()):
        if r.ok:
            print(f"OK    {key:16} latest {r.as_of} = {r.display_value}{'  (STALE)' if r.stale else ''}")
        else:
            bad.append(f"{key}: {r.error}")
            print(f"FAIL  {key:16} {r.error}")
    for name, err, ok_text in (
        ("FDIC bank failures", b.failures.error, f"{len(b.failures.failures)} records"),
        ("Release calendar", b.calendar.error, f"{len(b.calendar.events)} events in the next 7 days"),
    ):
        print(f"{'FAIL' if err else 'OK  '}  {name:20} {err or ok_text}")
        if err:
            bad.append(f"{name}: {err}")
    print(f"\n{len(bad)} source(s) failed.")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Results as run annotations, visible on the run page without opening the log.
        oks = [k for k, r in sorted(b.readings.items()) if r.ok]
        print(f"::notice title=Sources OK ({len(oks)})::{', '.join(oks)}")
        for line in bad[:9]:
            print(f"::warning title=Source failed::{line[:400].replace('%', '%25')}")
    core_ok = sum(1 for r in b.readings.values() if r.ok) >= len(b.readings) * 0.8
    return 0 if core_ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="write files instead of emailing")
    ap.add_argument("--sample", action="store_true", help="use made-up sample data (implies --dry-run)")
    ap.add_argument("--check-series", action="store_true", help="report which sources load, then exit")
    ap.add_argument("--out-dir", default=".", help="where --dry-run writes its files")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    today = fetch.today_eastern()
    if args.sample:
        from rates_digest.sample_data import sample_bundle
        b = sample_bundle(today)
    else:
        b = collect(today)

    if args.check_series:
        return check_sources(b)

    loaded = sum(1 for r in b.readings.values() if r.ok)
    if loaded < len(b.readings) / 2:
        log.error("Only %d of %d readings loaded -- not sending a mostly empty email.", loaded, len(b.readings))
        return 1

    rep = build_report(b)
    images = {c.cid: c.png for a in rep.areas for c in a.charts}
    html_body, text_body, subject = render_html(rep, b), render_text(rep), rep.subject
    pdf_bytes = render_pdf(rep)
    pdf_name = f"IntraFi-Risk-Monitor-{today:%Y-%m-%d}.pdf"

    if args.dry_run or args.sample:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        preview = html_body
        for cid, png in images.items():   # point the saved HTML at the saved PNGs so it opens in a browser
            (out / f"{cid}.png").write_bytes(png)
            preview = preview.replace(f'"cid:{cid}"', f'"{cid}.png"')
        (out / "newsletter.html").write_text(preview, encoding="utf-8")
        (out / "newsletter.txt").write_text(text_body, encoding="utf-8")
        (out / pdf_name).write_bytes(pdf_bytes)
        print(f"Subject: {subject}\nWrote {out / 'newsletter.html'} (+ {len(images)} charts) and {out / pdf_name}")
        return 0

    send_email(EmailConfig.from_env(), subject, text_body, html_body, images,
               attachments=[(pdf_name, pdf_bytes, "pdf")])
    log.info("Sent: %s", subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
