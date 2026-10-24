# Rates & Deposit Risk Monitor

A five-page daily report, emailed every weekday morning (about 8:45am Eastern)
with the same report attached as a PDF.

| Page | Contents |
|---|---|
| 1 Summary | Risk / Watch / Normal counts, key signals, every measure in one table (latest, last change, 1 month, 1 year, status) |
| 2 Rates | Fed policy path, Treasuries vs. bank deposits, 10-year Treasury, yield curve |
| 3 Deposits & competition | Deposit flight (small vs. largest banks vs. money funds), money market funds, stablecoins, small-bank borrowing |
| 4 Funding & credit stress | Overnight funding (SOFR − IORB), discount window, high-yield spread, CRE loans past due |
| 5 Market stress & calendar | VIX, bank failures, upcoming Fed decisions and data releases, data sources |

Each chart panel shows the latest reading, a status badge and a one-line
signal; Watch and Risk ranges are shaded on the chart. Every reading links to
its source so it can be checked. The PDF is always exactly five pages (tested).

Status ranges live in `ITEMS` in `rates_digest/newsletter.py` (rules of thumb,
easy to adjust). Each measure there also carries a plain-English "what it is"
and "why it matters" note, kept for a separate reference guide.

## Sources

FRED (St. Louis Fed) for rates and bank data, FDIC BankFind for bank failures,
DeFiLlama for stablecoins, and the Fed's published meeting schedule (in
`rates_digest/extras.py`; add next year's dates each fall, and the report warns
two months before the list runs out). FHLB advance rates are member-only;
small-bank borrowing is the closest public read on FHLB demand.

## Setup

Secrets on this repo: `EMAIL_ADDRESS` and `EMAIL_APP_PASSWORD` (shared with the
FFIEC monitor), `FRED_API_KEY`, optionally `ALERT_RECIPIENT`.

Actions → *Rates & Deposit Risk Monitor* → *Run workflow*: `check_series`
(does every source load), `dry_run` (build the email and PDF as a downloadable
artifact), or `send`.

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
  monitor.py       the five-page layout: summary, chart pages, signals, email HTML
  pdf.py           the five-page PDF from the same content
  newsletter.py    measures: status ranges, notes, source links
  charts.py        PNG charts with shaded Watch/Risk ranges
  series.py        FRED series
  analysis.py      changes, past-year ranges, spreads
  fetch.py         FRED
  extras.py        FDIC bank failures, stablecoins, calendar and Fed meeting dates
  email_report.py  Gmail sending (charts inline, PDF attached)
  sample_data.py   made-up data for previews and tests
run.py             entry point
tests/
```
