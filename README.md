# FFIEC Call Report Monitor

Daily automated check of FFIEC Call Report filings across FDIC-insured institutions:
new filings, quiet refilings/restatements, deteriorating risk ratios, and a standing
Watch List of already-fragile banks. Full design in
[`ffiec-monitor-spec.md`](./ffiec-monitor-spec.md) -- this README is setup/ops only.

## Layout

```
ffiec_monitor/
  config.py        secrets/env wiring (fail loudly if missing, never a silent default)
  ffiec_client.py  FFIEC CDR PWS REST client (OAuth2 bearer)
  db.py            DuckDB schema + storage
  ratios.py        the 12 ratios of concern, extraction + diffing + materiality floor
  watch_list.py    LTWC / enforcement-order Watch List qualification
  alerts.py        Tier 1/2/3 classification, restatement handling, escalation rule
  email_report.py  email structure + smtplib delivery
  pipeline.py       daily-run orchestration
run.py             CLI entrypoint
scripts/
  import_enforcement_orders.py   loads the (separately maintained) enforcement-orders CSV
  fake_diff.py                   synthetic Tier 1 event to test the email path (§10.2)
  svb_backtest.py                historical validation against SVB (§10.3)
tests/             unit tests (ratio diffing, materiality floor, Watch List, tiers)
.github/workflows/daily.yml      scheduled run
```

## Secrets (GitHub Actions: Settings -> Secrets and variables -> Actions)

| Secret | Notes |
|---|---|
| `FFIEC_USER_ID` | **Not explicitly listed in the spec's §9 secrets table, but required** -- the PWS auth scheme (§1) is a `UserID` header *plus* a bearer token, so both credentials are needed. Flagging this rather than quietly assuming a workaround. |
| `FFIEC_TOKEN` | 90-day JWT, does **not** auto-refresh. See "Known limitations" below. |
| `EMAIL_ADDRESS` | Sender and (default) recipient -- Gmail-to-Gmail for the personal-account phase (§8). |
| `EMAIL_APP_PASSWORD` | Google Account App Password, not the real password. |

`ALERT_RECIPIENT` is an optional env var/secret if you ever want the recipient to
differ from `EMAIL_ADDRESS`; defaults to `EMAIL_ADDRESS` per spec §8.

## Populating the enforcement-orders reference table

The enforcement-orders system of record lives in a separate, already-built project
(spec §3/§4) that this repo has no access to. Export it to CSV
(`id_rssd, fdic_cert, order_type, status, effective_date, terminated_date`, with
`order_type` in `SAFETY_SOUNDNESS` / `AML_BSA` / `CMP` and `status` in `ACTIVE` /
`TERMINATED`) and load it:

```
python scripts/import_enforcement_orders.py path/to/enforcement_orders.csv
```

Re-run this whenever the source system's export changes -- it's a full replace, not
an incremental sync.

## Risk-ranking reference list

`data/risk-rankings.csv` is a third-party risk-score ranking (name/state/assets/
percentile/CRITICAL-HIGH tier) -- **not** enforcement-orders data, and it has no
RSSD ID. Load it into its own `risk_rankings` table (separate from
`enforcement_orders`, since a risk-score tier isn't a S&S/AML/CMP order and
doesn't by itself feed spec §4's Watch List logic):

```
python scripts/import_risk_rankings.py data/risk-rankings.csv
```

Each row needs to be resolved to an `id_rssd` (the FFIEC join key) before it can
drive an actual pipeline run -- `db.unresolved_risk_rankings()` lists what's still
pending. Note at least one name+state collision in the source data (two distinct
"Farmers Savings Bank, IA" entries), so name+state alone isn't a safe resolution key.

## Running

```
pip install -r requirements.txt

# 1. Small known list first (the enforcement-orders banks) -- spec §10.1
python run.py --universe small

# 2. Prove the email path works end to end without hitting the API -- spec §10.2
python run.py --fake-diff

# 3. Historical validation against SVB -- spec §10.3 (needs a live token; see the
#    entity-scoping warning in the script's docstring before trusting the output)
python scripts/svb_backtest.py

# 4. Only once the small-list run is trusted, widen to the full universe
python run.py --universe full
```

`--no-send` builds and prints the email body without actually emailing it, for
dry runs.

Unit tests: `python -m pytest tests/`

## Scheduling

`.github/workflows/daily.yml` runs on a daily cron plus `workflow_dispatch` for
manual triggering (do this once by hand after the first push, per spec §9, before
trusting the unattended schedule). The `*.duckdb` state file is gitignored (spec §9)
and instead persists across runs via `actions/cache` -- there is no repo commit of
the database.

## Known limitations (spec §11)

- **90-day token expiry, no auto-refresh, manual renewal.** The code fails loudly on
  an auth error (`FFIECAuthError` propagates all the way out of `run.py`) as a
  backstop, but that is not a substitute for a separate calendar reminder.
- Live FFIEC data only -- no FreeNIC or other third-party dataset, no licensing
  considerations apply here.
- The materiality floor (5bp ratios / ~1% relative dollar figures) is a starting
  point; expect to retune `RATIO_MATERIALITY_FLOOR_PP` / `DOLLAR_MATERIALITY_RELATIVE_FLOOR`
  in `ratios.py` after a week or two of real output.
- `ffiec_client.py`'s endpoint names/params follow FFIEC CDR PWS's long-standing
  method names (`RetrieveFilersSubmissionDateTime`, `RetrieveFacsimile`,
  `RetrieveUBPRXBRLFacsimile`, `RetrieveReportingPeriods`) but have not been verified
  against a live token in this build environment, and neither has the UBPR
  field-tag mapping in `ratios.UBPR_FIELD_MAP`. If a real pull comes back shaped
  differently, those are the two places to fix -- the rest of the pipeline
  (diffing, tiers, Watch List, email) is independent of the wire format.
