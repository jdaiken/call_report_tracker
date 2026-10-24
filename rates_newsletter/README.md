# Weekly Rates & Markets

One email every Monday morning with the essential rates and market measures,
charts for each area, and for every item:

- **What it is**: one plain-English sentence
- **What it means now**: today's reading, its trend, and what it signals
- **Risk to watch**: why it matters for banks and IntraFi's network
- **Ranges to know**: rule-of-thumb zones (Normal / Watch / Risk) with the
  current one highlighted, plus the actual past-year range

The top of the email, "The week in one page," lists anything sitting in a Watch
or Risk range, Fed actions, bank failures, and how markets moved.

Ranges are general rules of thumb for context, not official thresholds. They
live in `ITEMS` in `rates_digest/newsletter.py` and are easy to adjust.

## What it tracks

| Section | Items | Charts |
|---|---|---|
| The Fed and overnight funding | Fed funds target, effective fed funds, SOFR, SOFR minus IORB | Fed target vs. EFFR vs. SOFR |
| Treasury yields | 3-month bill, 2-year, 10-year | All three, 2 years |
| The yield curve | 10-year minus 2-year | 5 years |
| What banks pay vs. Treasuries | FDIC national money market and 12-month CD rates; T-bill vs. money market gap | Deposit rates vs. bills; the gap |
| Where deposits are going | Small-bank and large-bank deposits, retail money market funds | Indexed to a year ago |
| Bank funding stress | Small-bank borrowings (mostly FHLB), Fed discount window | One chart each |
| Credit risk | High-yield spread, CRE loans past due | One chart each |
| Stocks and volatility | S&P 500, Dow, Nasdaq, VIX | Indexed stocks; VIX |
| The economy | CPI inflation, unemployment | Both, 5 years |
| The week ahead | Fed decisions, CPI, jobs, GDP, PCE release dates | |

## Sources

FRED (St. Louis Fed) for every rate and market series, FDIC BankFind for bank
failures, and the Fed's published meeting schedule (typed into
`rates_digest/extras.py`; add next year's dates each fall, and the email warns
two months before the list runs out).

FHLB advance rates aren't included: the FHLBanks publish them to members only.
Small-bank borrowings are the closest public read on FHLB demand.

## Setup

Secrets on this repo (Settings → Secrets and variables → Actions):
`EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` (shared with the FFIEC monitor),
`FRED_API_KEY`, and optionally `ALERT_RECIPIENT`.

Actions → *Weekly Rates & Markets* → *Run workflow*:
- `check_series`: lists every source as OK or failed (also shown as notes on the run page)
- `dry_run`: builds the email and attaches it as a downloadable artifact
- `send`: emails it now

## Running locally

```bash
pip install -r requirements.txt
python run.py --sample          # made-up data, no network; writes newsletter.html + charts
python run.py --check-series    # live: does every source load?
python -m pytest -q
```

## Layout

```
rates_digest/
  series.py        the essential series: what each is and the risk to watch
  newsletter.py    sections, charts, ranges to know, "what it means now", email layout
  analysis.py      changes, past-year ranges, spreads, automatic watch flags
  charts.py        PNG line charts (matplotlib)
  fetch.py         FRED
  extras.py        FDIC bank failures; release calendar and Fed meeting dates
  email_report.py  page shell and Gmail sending (charts inline)
  sample_data.py   made-up data for previews and tests (always labeled SAMPLE)
run.py             entry point
tests/
```

Not investment advice; an automated summary for personal reference.
