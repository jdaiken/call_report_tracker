# Rates & Markets Daily

A weekday-morning email that explains, in plain English, the interest rates and
market trends that matter to IntraFi and to banking generally. Each rate shows
today's value, how it compares with the previous reading, a month ago and a year
ago, where it sits in its one-year range, a small text chart of the past year,
and short "what it is / why it matters" notes.

Lives inside the call_report_tracker repo so it shares the FFIEC monitor's
email secrets. Its schedule is `.github/workflows/rates_digest.yml` at the repo
root; the code is in this folder.

## What's in it

| Section | Rates |
|---|---|
| Fed policy rates | Fed funds target range, effective fed funds (EFFR), interest on reserves (IORB), SOFR, prime |
| What banks pay depositors | FDIC national rates for savings, money market, 6- and 12-month CDs; FDIC rate caps |
| Where large depositors can put cash instead | 1-, 3- and 6-month T-bills, 1-year Treasury, retail money market fund assets |
| Bank deposits and funding | Deposits at all, large and small banks; large CDs at small banks; reserves; Fed reverse repo |
| Spreads that matter for IntraFi | T-bill vs. bank money market and CD rates, T-bill vs. fed funds and IORB, SOFR vs. IORB, yield curve |
| General market trends | 2- and 10-year Treasuries, mortgages, credit spreads, VIX, S&P 500, oil, dollar, inflation, unemployment, bank loans |
| From the Fed this week | Monetary-policy and bank-regulation press releases from the past 7 days |

The top of each email has a "Today in 30 seconds" summary with automatic
**Watch** flags, for example: a Fed rate change, an inverted yield curve,
deposits falling at small banks while rising at large ones, SOFR above IORB,
credit spreads widening, or the T-bill vs. bank deposit gap widening.

All data comes from FRED (Federal Reserve Bank of St. Louis), which republishes
the Fed, New York Fed, Treasury, FDIC, Freddie Mac and ICE BofA figures. FHLB
advance rates are not included because there is no free public feed.

## Setup

1. **Secrets** (Settings → Secrets and variables → Actions). If this repo lives
   next to the FFIEC monitor, reuse the same values.

   | Secret | Required | Notes |
   |---|---|---|
   | `EMAIL_ADDRESS` | yes | Gmail address the digest is sent from |
   | `EMAIL_APP_PASSWORD` | yes | Gmail app password (not your normal password) |
   | `ALERT_RECIPIENT` | no | Where to send it; defaults to `EMAIL_ADDRESS` |
   | `FRED_API_KEY` | recommended | Free at fred.stlouisfed.org → My Account → API Keys. Without it the job uses FRED's public CSV downloads, which can be rate-limited from GitHub's servers. |

2. **First run:** Actions → *Daily Rates & Markets Digest* → *Run workflow* →
   mode `check_series`. Every line should say `OK`. Then run `dry_run` and
   download the `digest-preview` artifact to see the email, then `send`.

3. After that it runs automatically every weekday at about 8:45am Eastern.

## Running locally

```bash
pip install -r requirements.txt
python run.py --sample          # made-up data, no network; writes digest.html
python run.py --dry-run         # live data, writes digest.html, no email
python run.py --check-series    # confirm every series loads
python -m pytest -q             # tests
```

## Changing what's tracked

Rates, explanations and "notable move" sizes live in `rates_digest/series.py`.
Watch-flag rules live in `watch_flags()` in `rates_digest/analysis.py`. To add a
FRED series, add a `Series(...)` entry with its FRED ID; nothing else needs to
change.

## Layout

```
rates_digest/
  series.py        every rate tracked, with plain-English explanations
  fetch.py         FRED (API or CSV) and Fed RSS; a failed series never stops the email
  analysis.py      trend comparisons, ranges, spreads, watch flags
  email_report.py  HTML + plain-text email, Gmail SMTP
  sample_data.py   made-up data for previews and tests (always labeled SAMPLE)
run.py             entry point
tests/             unit tests
```
(The workflow file is `../.github/workflows/rates_digest.yml`.)

Not investment advice; an automated summary for personal reference.
