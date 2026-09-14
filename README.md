# FFIEC Call Report Monitor

Daily automated check of FFIEC Call Report filings across FDIC-insured institutions:
new filings, quiet refilings/restatements, deteriorating risk ratios, and a standing
Watch List of already-fragile banks. Full design in
[`ffiec-monitor-spec.md`](./ffiec-monitor-spec.md) -- this README is setup/ops only.

## Layout

```
ffiec_monitor/
  config.py        secrets/env wiring (fail loudly if missing, never a silent default)
  ffiec_client.py  FFIEC CDR PWS REST client (OAuth2 bearer), written against the
                   real SIS611 API doc -- see "Known limitations" for the one
                   remaining unverified piece (SDF facsimile layout)
  db.py            DuckDB schema + storage
  ratios.py        the 12 ratios of concern, extraction from Call Report MDRM fields,
                   diffing + materiality floor (see its module docstring for which
                   ratios are direct reads vs. approximations, and why)
  matching.py      conservative name+state -> RSSD matching (shared by both resolvers)
  watch_list.py    LTWC / enforcement-order Watch List qualification
  alerts.py        Tier 1/2/3 classification, restatement handling, escalation rule
  email_report.py  email structure + smtplib delivery
  pipeline.py       daily-run orchestration
run.py             CLI entrypoint
scripts/
  import_enforcement_orders.py   loads the (separately maintained) enforcement-orders CSV
  import_risk_rankings.py        loads data/risk-rankings.csv (no RSSD yet)
  resolve_from_bulk_por.py       resolves risk_rankings to RSSD from a bulk POR file (offline)
  resolve_risk_rankings.py       same, but against the live FFIEC API (no bulk file on hand)
  apply_rssd_map.py              re-applies the already-resolved data/risk-rankings-rssd-map.csv
  fake_diff.py                   synthetic Tier 1 event to test the email path (§10.2)
  svb_backtest.py                historical validation against SVB (§10.3)
tests/             unit tests (ratio extraction/diffing, materiality floor, Watch List, tiers)
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
percentile/CRITICAL-HIGH tier) for 200 banks -- **not** enforcement-orders data.
It's stored in its own `risk_rankings` table (separate from `enforcement_orders`,
since a risk-score tier isn't a S&S/AML/CMP order and doesn't by itself feed spec
§4's Watch List logic), and it ships with no RSSD ID -- that's the FFIEC join key
everything else depends on, so it has to be resolved once:

```
python scripts/import_risk_rankings.py data/risk-rankings.csv
python scripts/apply_rssd_map.py   # applies the already-resolved mapping below
```

All 200 are already resolved to `id_rssd` in `data/risk-rankings-rssd-map.csv`
(committed, since it's tiny -- unlike the ~30MB bulk zip it was derived from,
which isn't). How that resolution happened, for when the list changes and this
needs redoing:

- Matched name+state against the Panel of Reporters (POR) file from a real FFIEC
  CDR bulk Call Report download (`scripts/resolve_from_bulk_por.py`) -- exact
  matches only auto-assign (see `ffiec_monitor/matching.py`).
- 13 name collisions (e.g. two distinct "Farmers Savings Bank, IA") were broken
  safely using a second, independent signal: comparing each candidate's real
  reported total assets against the risk-ranking's reported assets -- 12 had an
  unambiguous near-exact match; disambiguating by name alone would have been a
  guess, by assets it wasn't.
- One ("Increase Bank, WA") had no name match at all on the panel, but a
  "Twin City Bank, WA" with assets 0.03% off -- almost certainly a fintech
  sponsor-bank/DBA situation, confirmed with the user before assigning rather
  than auto-matched (a no-name-overlap match is a bigger leap than a same-name
  collision, even with a strong asset signal).

If the risk-ranking list changes, re-run `resolve_from_bulk_por.py` against a
fresh bulk download (or `resolve_risk_rankings.py` against the live API if you
don't have one on hand) and re-export `data/risk-rankings-rssd-map.csv`.

## Running

```
pip install -r requirements.txt

# 1. Small known list first (enforcement-orders banks + the 200 risk-ranking
#    banks, both resolved to RSSD) -- spec §10.1
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
manual triggering. The `*.duckdb` state file is gitignored (spec §9) and instead
persists across runs via `actions/cache` -- there is no repo commit of the database.

**Runner type is currently unresolved -- don't assume self-hosted is required.**
Every live call so far has failed identically (a generic Azure Application
Gateway 403 HTML page, not FFIEC's documented JSON error shape) regardless of
runner type (GitHub-hosted vs. this repo's self-hosted Windows runner) or which
of two token states was in the secret -- so neither "GitHub's IPs are blocked"
nor "the token was bad" is actually confirmed; an earlier version of this README
claimed the IP-block theory was confirmed by a personal-network test, but that
test was never actually run, so that line was wrong and has been removed.

One concrete, previously-unaddressed candidate was found and fixed: the client
was sending `requests`' default `User-Agent` (`python-requests/x.y.z`), a
signature Azure's WAF (OWASP Core Rule Set "known bad UA" rules) commonly blocks
at the gateway before APIM or FFIEC's own logic ever runs -- which would produce
exactly this symptom (generic gateway HTML 403) independent of token validity
*and* independent of source IP, matching every failure seen on both runner types.
`ffiec_client.py` now sends a real `User-Agent` (see `USER_AGENT` in that file)
and an explicit `Accept: application/json`. This needs a real
`workflow_dispatch` run to confirm before drawing any conclusion about whether
a self-hosted runner is needed at all -- try `ubuntu-latest` first now that this
fix is in, since it's simpler to operate if it works.

### Self-hosted runner setup

1. Repo → **Settings → Actions → Runners → New self-hosted runner**, pick your OS,
   and follow GitHub's generated `config.sh`/`config.cmd` commands on the machine
   you want the job to actually run from (needs a network FFIEC's gateway accepts
   -- i.e. not another cloud VM's datacenter IP, which is very likely to hit the
   same block).
2. Run it as a service (the setup script offers `svc.sh install && svc.sh start`
   on Linux/macOS, or an equivalent on Windows) so it's listening even when no
   one's logged in -- the scheduled run only fires if the runner is online and
   idle at 13:00 UTC.
3. That machine needs Python (3.9+) already installed and on `PATH` -- the
   workflow uses it directly rather than `actions/setup-python`, since that
   action's own Windows installer needs admin rights to write to the registry,
   which fails on a non-elevated runner. It also needs to stay on and
   network-connected for the daily schedule to actually execute (this is the
   real tradeoff of a self-hosted runner -- it depends on that machine's
   uptime, unlike GitHub's hosted runners).
4. Trigger a manual `workflow_dispatch` run once to confirm it picks up the
   self-hosted runner and completes before trusting the unattended schedule.

## Known limitations (spec §11)

- **90-day token expiry, no auto-refresh, manual renewal.** The code fails loudly on
  an auth error (`FFIECAuthError` propagates all the way out of `run.py`) as a
  backstop, but that is not a substitute for a separate calendar reminder.
- Live FFIEC data only -- no FreeNIC or other third-party dataset, no licensing
  considerations apply here.
- The materiality floor (5bp ratios / ~1% relative dollar figures) is a starting
  point; expect to retune `RATIO_MATERIALITY_FLOOR_PP` / `DOLLAR_MATERIALITY_RELATIVE_FLOOR`
  in `ratios.py` after a week or two of real output.
- `ffiec_client.py` is now written against FFIEC's actual API documentation
  ("SIS611 Retrieve Public Data via Web Service", CDR-PDD-SIS-611 v1.10), not
  guessed -- the first live run against an earlier, guessed version failed
  instantly with a generic Azure Application Gateway 403 HTML page (not FFIEC's
  own JSON error shape), consistent with the request never reaching FFIEC's API
  logic at all. Three things the guess got wrong, now fixed and covered by
  `tests/test_ffiec_client.py`: every method parameter is a header, not a query
  param; dates are `MM/DD/YYYY`, not ISO; and `RetrieveFacsimile` returns a raw
  SDF byte-stream (`{"FacsimileFile": <base64>}`), not pre-parsed JSON fields --
  `_parse_sdf()` decodes and parses it. One real caveat remains: FFIEC's own doc
  has no actual SDF content sample, so `_parse_sdf()`'s layout (a header row of
  MDRM codes + one data row, matching the bulk download's convention) is an
  educated guess, not confirmed against a live single-institution pull -- if
  that's wrong, `_parse_sdf()` is the one place to fix, not `ratios.py`'s
  field-level mapping (which *is* verified, against a real FFIEC CDR bulk Call
  Report download -- see `ratios.py`'s module docstring for per-ratio confidence:
  capital ratios and most balance-sheet ratios are direct reads; ROAA/NIM/
  charge-off annualize a single period as an approximation of a true multi-quarter
  average; Net Non-Core Funding Dependence is a simplified stand-in for UBPR's
  maturity-bucketed formula; peer-average charge-off isn't computed at all).
- `RetrieveFilersSubmissionDateTime` requires a `lastUpdateDateTime` cursor
  (FFIEC's recommended usage, SIS611 §3.1) -- tracked per reporting period in the
  new `poll_state` table so each run only asks for what changed since the last
  one, rather than re-fetching every filer's status every day.
