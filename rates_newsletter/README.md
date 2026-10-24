# Rates & Markets Newsletter

Two emails, built from the same data:

- **Daily email (Tuesday–Friday, ~2 minutes):** "Today in 30 seconds" (Fed range,
  how stocks moved, regional banks, any **Watch** flags), a 12-row key-numbers
  table, short notes on anything that moved enough to matter, what's coming up
  this week, Fed headlines, and every other rate on one line each.
- **Weekly look book (Mondays, ~15 minutes):** the week in one page, 12 charts
  each with a plain-English analysis, regional bank stocks (weakest five), the
  week ahead, and definitions of every rate.

Lives inside the call_report_tracker repo so it shares the FFIEC monitor's email
secrets. The schedule is `.github/workflows/rates_digest.yml` at the repo root.

## What it tracks

| Area | What |
|---|---|
| Fed policy rates | Fed funds target range, effective fed funds, IORB, SOFR, prime |
| What banks pay depositors | FDIC national rates (savings, money market, 6- and 12-month CDs) and rate caps |
| Alternatives for large depositors | 1-, 3-, 6-month T-bills, 1-year Treasury, retail money market funds, stablecoin supply (look book) |
| Bank deposits and funding | Deposits at all, large and small banks; large CDs at small banks; reserves; reverse repo |
| Bank stress signals | Fed discount window borrowing, small-bank borrowings (mostly FHLB), Treasury's cash account, CRE delinquencies, FDIC bank failures, regional bank stocks |
| Spreads | T-bill vs. bank money market and CD rates, T-bill vs. fed funds and IORB, SOFR vs. IORB, yield curve |
| Markets | 2-, 5-, 10-, 30-year Treasuries, mortgages, credit spreads, VIX, S&P 500, Dow, Nasdaq, oil, dollar, inflation, unemployment, bank loans |
| Calendar | Fed meeting dates, CPI, jobs, GDP, PCE and other key releases in the coming week |

Stress items stay quiet unless something crosses a threshold (for example, a bank
failure in the past week, a jump in discount window borrowing, or regional bank
stocks falling 3% or more in a day).

**Not included:** FHLB advance rates. The FHLBanks publish them to members only;
small-bank borrowings are the closest public read on FHLB demand.

## Sources

| Source | Used for | Key needed |
|---|---|---|
| FRED (St. Louis Fed) | almost all rates and series, plus the release calendar | `FRED_API_KEY` (free; calendar needs it) |
| FDIC BankFind API | bank failures | no |
| DeFiLlama | stablecoin supply | no |
| Finviz via `finvizfinance` | regional bank stocks | no |
| federalreserve.gov | Fed press release feeds; FOMC dates (typed into `extras.py`) | no |

**Finviz is turned off** (October 2026): Finviz redesigned its screener and
`finvizfinance` can't read the new pages yet. To try it again later, add
`finvizfinance` to requirements.txt and set `ENABLE_FINVIZ: "1"` in the workflow's
env. About Finviz: `finvizfinance` is an unofficial package that reads Finviz's public
web pages. It can break when Finviz changes its site or blocks automated requests,
and you should check Finviz's terms of use. If it fails, the emails still go out
without the regional bank section. Finviz Elite's official API is the dependable
alternative.

**Once a year:** add the next year's Fed meeting dates to `FOMC_DECISION_DAYS` in
`rates_digest/extras.py`. The email warns you two months before the list runs out.

## Setup

Secrets (Settings → Secrets and variables → Actions on this repo): `EMAIL_ADDRESS`
and `EMAIL_APP_PASSWORD` (already used by the FFIEC monitor), `FRED_API_KEY`, and
optionally `ALERT_RECIPIENT`.

Actions → *Rates & Markets Newsletter* → *Run workflow*:
- mode `check_series` lists every source as OK or FAIL;
- mode `dry_run` builds the email (pick `daily` or `weekly`) and attaches it as a downloadable artifact;
- mode `send` emails it.

## Running locally

```bash
pip install -r requirements.txt
python run.py --sample --edition daily     # made-up data, no network
python run.py --sample --edition weekly    # look book preview with charts
python run.py --check-series               # live: does every source load?
python -m pytest -q
```

## Layout

```
rates_digest/
  series.py        every FRED series tracked, with plain-English explanations
  fetch.py         FRED and Fed RSS
  extras.py        FDIC failures, stablecoins, calendar + FOMC dates, Finviz regional banks
  analysis.py      trends, ranges, spreads, watch flags, "what moved"
  email_report.py  daily email, shared layout pieces, Gmail sending (inline chart images)
  lookbook.py      weekly look book and its rules-based analysis
  charts.py        PNG line charts (matplotlib)
  sample_data.py   made-up data for previews and tests (always labeled SAMPLE)
run.py             entry point
tests/
```

Not investment advice; an automated summary for personal reference.
