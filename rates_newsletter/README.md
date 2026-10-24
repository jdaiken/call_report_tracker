# IntraFi Risk Monitor (rates & markets)

One email every weekday morning (about 8:45am Eastern), with the same report
attached as a PDF. It tracks only the rate and market measures that signal a
risk IntraFi cares about.

**How it reads, top to bottom:**
1. **Today's alerts:** anything in a Watch or Risk range, a Fed rate move, or a bank failure in the past week.
2. **Risk dashboard:** every measure on one line with a status (Normal / Watch / Risk / Info), a one-line insight and today's reading.
3. **One section per risk area**, each with charts (Watch and Risk ranges shaded) and, for every measure:
   **Insight** (what today's reading means), **Why IntraFi cares** (the risk), **What it is**, and **Ranges**.
4. **Coming up:** Fed decisions and major data releases in the next week.

Ranges are general rules of thumb for context, not official thresholds. They
live in `ITEMS` in `rates_digest/newsletter.py` and are easy to adjust.

## What it tracks

| Risk area | Measures |
|---|---|
| Rate path | Fed funds rate, rate outlook (2-year Treasury) |
| Deposit competition | T-bill vs. average bank money market rate, retail money market funds |
| Deposit flight | Small-bank deposits (vs. the largest banks) |
| Stablecoins | Dollar stablecoins in circulation |
| Funding and liquidity stress | SOFR minus interest on reserves, Fed discount window, small-bank borrowing (mostly FHLB) |
| Member-bank balance sheets | 10-year Treasury, yield curve, CRE loans past due |
| Credit and market stress | High-yield spread, VIX |
| Bank failures | FDIC failures this year and in the past week |

FHLB advance rates aren't included: the FHLBanks publish them to members only.
Small-bank borrowing is the closest public read on FHLB demand.

## Sources

FRED (St. Louis Fed) for rates and bank data, FDIC BankFind for bank failures,
DeFiLlama for stablecoins, and the Fed's published meeting schedule (typed into
`rates_digest/extras.py`; add next year's dates each fall, and the email warns
two months before the list runs out).

## Setup

Secrets on this repo (Settings → Secrets and variables → Actions):
`EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` (shared with the FFIEC monitor),
`FRED_API_KEY`, and optionally `ALERT_RECIPIENT`.

Actions → *IntraFi Risk Monitor* → *Run workflow*:
- `check_series`: lists every source as OK or failed (also shown as notes on the run page)
- `dry_run`: builds the email and PDF and attaches them as a downloadable artifact
- `send`: emails it now

## Running locally

```bash
pip install -r requirements.txt
python run.py --sample          # made-up data, no network: newsletter.html, charts and the PDF
python run.py --check-series    # live: does every source load?
python -m pytest -q
```

## Layout

```
rates_digest/
  newsletter.py    risk areas, measures (insight / why IntraFi cares / what it is / ranges), report + email
  pdf.py           the PDF attachment, drawn from the same report
  charts.py        PNG line charts with shaded Watch/Risk ranges
  series.py        the FRED series used
  analysis.py      changes, past-year ranges, spreads, sudden-move flags
  fetch.py         FRED
  extras.py        FDIC bank failures, stablecoins, release calendar and Fed meeting dates
  email_report.py  page shell and Gmail sending (charts inline, PDF attached)
  sample_data.py   made-up data for previews and tests (always labeled SAMPLE)
run.py             entry point
tests/
```

Not investment advice; an automated summary for personal reference.
