"""Daily rates & markets digest.

  python run.py                 fetch live data and email it
  python run.py --dry-run       fetch live data, write digest.html / digest.txt, don't email
  python run.py --sample        use made-up sample data (no network) and write the preview files
  python run.py --check-series  fetch every series once and print which ones loaded
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

from rates_digest import analysis, fetch
from rates_digest.email_report import EmailConfig, headline_for, render_html, render_text, send_email
from rates_digest.series import SERIES, SPREADS

log = logging.getLogger("rates_digest")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="write files instead of emailing")
    ap.add_argument("--sample", action="store_true", help="use synthetic sample data (implies --dry-run)")
    ap.add_argument("--check-series", action="store_true", help="report which series load, then exit")
    ap.add_argument("--out-dir", default=".", help="where --dry-run writes digest.html / digest.txt")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    today = fetch.today_eastern()
    ids = sorted({s.id for s in SERIES} | {sp.a for sp in SPREADS} | {sp.b for sp in SPREADS})

    if args.sample:
        from rates_digest.sample_data import sample_observations, sample_headlines
        data, headlines, feed_errors = sample_observations(today), sample_headlines(today), []
    else:
        data = fetch.fetch_all(ids, start=today - timedelta(days=800))
        headlines, feed_errors = fetch.fetch_headlines(since=today - timedelta(days=7))

    if args.check_series:
        bad = 0
        for sid in ids:
            o = data[sid]
            if o.points:
                print(f"OK    {sid:16} {len(o.points):5} obs, latest {o.points[-1][0]} = {o.points[-1][1]}  units={o.units}")
            else:
                bad += 1
                print(f"FAIL  {sid:16} {o.error}")
        print(f"\n{len(ids) - bad}/{len(ids)} series loaded; feeds missing: {feed_errors or 'none'}")
        return 1 if bad else 0

    loaded = sum(1 for o in data.values() if o.points)
    if loaded < len(ids) / 2:
        log.error("Only %d of %d series loaded -- not sending a mostly empty digest.", loaded, len(ids))
        return 1

    readings = analysis.build_all(data, today)
    flags = analysis.watch_flags(readings)
    moves = analysis.notable_moves(readings, covered=frozenset(k for f in flags for k in f.keys))
    used_api = bool(os.environ.get("FRED_API_KEY", "").strip()) and not args.sample

    html_body = render_html(today, readings, flags, headlines, feed_errors, used_api)
    text_body = render_text(today, readings, flags, headlines)
    subject = f"Rates & Markets Daily, {today:%a %b %-d}: {headline_for(flags, moves)}"

    if args.sample:
        banner = ('<div style="background:#fff4ce;color:#5c4400;padding:10px 16px;font:14px sans-serif;text-align:center">'
                  "SAMPLE DATA FOR PREVIEW ONLY. These numbers are made up, not real rates.</div>")
        html_body = html_body.replace("<body style=\"margin:0;padding:0;background:#f4f5f7\">",
                                      "<body style=\"margin:0;padding:0;background:#f4f5f7\">" + banner, 1)
        text_body = "*** SAMPLE DATA FOR PREVIEW ONLY - NOT REAL RATES ***\n\n" + text_body

    if args.dry_run or args.sample:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "digest.html").write_text(html_body, encoding="utf-8")
        (out / "digest.txt").write_text(text_body, encoding="utf-8")
        print(f"Subject: {subject}\nWrote {out / 'digest.html'} and {out / 'digest.txt'}")
        return 0

    send_email(EmailConfig.from_env(), subject, text_body, html_body)
    log.info("Sent: %s (%d/%d series loaded)", subject, loaded, len(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
