# FFIEC Call Report Monitoring Agent — Build Spec

**Purpose:** Daily automated check of FFIEC Call Report filings across all FDIC-insured
institutions, detecting new filings and refilings/restatements, flagging deterioration in
key risk ratios, and emailing alerts — with special handling for a standing "Watch List" of
already-fragile banks.

This document is the spec. Build against it section by section; nothing here should require
guessing at intent.

---

## 1. Data source

- **FFIEC CDR Public Web Service (PWS)**, REST API, OAuth2 bearer token (JWT).
- Auth: `UserID` header + `Authentication: Bearer <token>` header.
- Base URL: `https://ffieccdr.azure-api.us/public/<function>`
- **Token expires every 90 days and does NOT auto-refresh.** Store it in an environment
  variable / GitHub Actions secret (`FFIEC_TOKEN`), never in code. Build in a hard failure
  (not a silent skip) if the API returns an auth error, so an expired token surfaces as an
  alert itself rather than a quietly-empty daily run.
- Universe: all FDIC-insured institutions filing Call Reports (~4,000-4,500 currently).
  RSSD ID is the join key across everything (Call Report, UBPR, enforcement orders).

## 2. Two-tier check design (not a full pull every day for every bank)

1. **Daily lightweight ping**, all institutions: pull the latest filed period + submission/
   acceptance timestamp only. Cheap, fast, well under the 2,500 req/hour ceiling.
2. **Full pull + diff**, only for institutions whose timestamp changed since last check.
   This is what catches both new-quarter filings and quiet restatements of an
   already-seen period.

Store last-known state in DuckDB (see §3), not a flat file — this needs to scale to the
full institution universe, not just a handful of tracked banks.

## 3. Storage — DuckDB tables

```sql
-- last known snapshot per institution per period, for diffing
CREATE TABLE call_report_snapshots (
    id_rssd        VARCHAR,
    period_end     DATE,
    last_seen_ts   TIMESTAMP,   -- FFIEC's submission/acceptance timestamp
    ratios         JSON,        -- the 12 "ratios of concern", see §5
    checked_at     TIMESTAMP,   -- when WE last checked this
    PRIMARY KEY (id_rssd, period_end)
);

-- daily-recomputed standing list, see §4
CREATE TABLE watch_list (
    id_rssd        VARCHAR PRIMARY KEY,
    reason         VARCHAR,     -- 'LTWC' | 'ORDER' | 'BOTH'
    added_at       DATE,
    removed_at     DATE         -- NULL while still active
);

-- reference to the existing enforcement orders table (already built separately)
-- join key: id_rssd or fdic_cert
```

## 4. Watch List definition

Recomputed fresh every daily run. A bank qualifies if **either** is true:

- **LTWC (Less Than Well Capitalized):** fails any PCA well-capitalized threshold —
  Tier 1 Leverage < 5%, Total RBC < 10%, Tier 1 RBC < 8%, or CET1 < 6.5%.
- **Under an active order, type = Safety & Soundness or AML/BSA**, pulled from the existing
  enforcement orders table. **Exclude banks whose only active order is a CMP** — a bank with
  a CMP *and* a separate S&S/AML order still qualifies (the CMP alone doesn't).

Membership is a status, re-evaluated daily, not a one-time flag. Track `added_at` /
`removed_at` so entering/exiting the list is itself detectable and gets called out in the
email (§7).

## 5. Ratios of concern (the 12-ratio cheat sheet)

| Category | Ratio | UBPR Location |
|---|---|---|
| Capital | Tier 1 Leverage Ratio | Capital Analysis |
| Capital | Tier 1 Risk-Based Capital Ratio | Capital Analysis |
| Capital | Total Risk-Based Capital Ratio | Capital Analysis |
| Liquidity | Net Non-Core Funding Dependence Ratio | Liquidity |
| Liquidity | Net Loans & Leases to Deposits | Liquidity |
| Liquidity | Pledged Securities Ratio | Liquidity |
| Asset Quality | Charge-off Ratio vs. Peer Average | Asset Quality |
| Asset Quality | Loan Growth vs. Capital Growth | Asset Quality |
| Earnings | ROAA | Summary Ratios—Page 1 |
| Earnings | Net Interest Margin | Summary Ratios—Page 1 |
| Deposit Composition | Core Deposits to Total Deposits | Liquidity / RC-E |
| Deposit Composition | Brokered/Reciprocal Deposits to Total Deposits | Liquidity / RC-E |

**Two ratios need special handling, not a simple old-vs-new line:**
- *Charge-off ratio vs. peer average* — show both: our ratio (old→new) AND current peer
  average for context (peer average doesn't need its own old→new).
- *Loan growth vs. capital growth* — these are two separate growth rates being compared to
  each other. Show as two rows: Loan Growth (old→new) and Capital Growth (old→new), not one
  combined line.

## 6. Alert tiers and thresholds

**Tier 1 — same-day, per-event email:**
- Leverage ratio crosses below 5% / 4% / 3% (PCA tier boundaries)
- Total RBC crosses below 10% / 8% / 6% (PCA tier boundaries)
- ROAA goes negative for the quarter
- Noncurrent loans+OREO ratio doubles QoQ, OR crosses ~3% absolute
- **A refiling restates a previously-reported capital ratio down >50bp** (this is the
  "quiet restatement" case — check diffs against already-seen periods, not just new periods)
- **Escalation rule:** any Tier 2 trigger (below) on a bank already on the Watch List
  auto-escalates to Tier 1.

**Tier 2 — daily digest, batched:**
- Leverage or Total RBC down >100bp QoQ, even while still above well-capitalized line
- Efficiency-adjacent stress: NIM compression, ROAA below 0.50% for 2+ consecutive quarters
- Total assets growth rate below -10% annualized
- Loans/deposits ratio up sharply QoQ

**Tier 3 — logged only, no email:**
- New quarter filed on schedule, nothing threshold-crossing
- Revisions below materiality floor: **5bp for ratios, ~1% relative for dollar figures**
  (this floor exists specifically to keep rounding/reclassification noise out of Tier 1/2)

## 7. Email structure

Every bank mentioned anywhere gets this identity block:

```
[Bank Name] — [City, State]
FDIC Cert #[XXXXX] · RSSD [XXXXX] · Total Assets: $[X.XB/M] (as of [period])
```

Full email, in order:

1. **Watch List changes** — "Added: [Bank] — reason" / "Removed: [Bank] — reason", top of
   email, always shown if any occurred.
2. **Tier 1 alerts** — one block per triggering bank/event:
   - Identity block
   - Old→new table for whichever of the 12 ratios tripped (not the full 12 every time —
     only the ones that fired)
   - If this bank is also on the Watch List: full current-ratio snapshot (all 12 ratios)
     included inline, so the email is self-contained without cross-referencing the digest
3. **Watch List ratio snapshot** — every bank currently on the list, every day, regardless
   of whether anything changed: identity block + current value for all 12 ratios. This is
   the standing visibility section.
4. **Tier 2 digest** — batched, one line per bank/trigger, identity block per bank.

If there's nothing to report in a section, omit that section entirely rather than showing
"none."

## 8. Delivery

- `smtplib`, Gmail (johnaiken4@gmail.com) sending **to that same Gmail address** — Gmail-to-Gmail for now, personal-account phase. Auth via a Google Account **App Password** (not the real password), stored as a GitHub Actions secret, never hardcoded.
- Secrets: `EMAIL_ADDRESS` (johnaiken4@gmail.com, used as both sender and `ALERT_RECIPIENT`), `EMAIL_APP_PASSWORD`.
- Tier 1 sends immediately, one email per triggering event (or batched if multiple fire in
  the same run — use judgment, but don't let a bad day across many banks turn into dozens
  of separate emails; group same-run Tier 1s into one email).
- Tier 2 sends once per day as a single digest, even if empty of new triggers, as long as
  the Watch List snapshot (§7.3) has content.
- Revisit sender/recipient once this moves toward an org setup (see §9) — Gmail-to-Gmail
  is the personal-phase default, not the permanent answer.

## 9. Scheduling

- **Repo: create a new, private, personal GitHub repository for this project** (e.g.
  `ffiec-call-report-watch`) — separate from any other project, personal account for now
  per the personal-first build decision. `git init` locally, `.gitignore` the `*.duckdb`
  file, push this spec doc as the first commit before any code.
- GitHub Actions, `on: schedule` cron trigger, daily.
- Secrets in repo Settings → Secrets and variables → Actions: `FFIEC_TOKEN`,
  `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`.
- Manually trigger one run from the Actions tab after first push, to confirm the whole
  pipeline works, before trusting the schedule unattended.

## 10. Testing plan before going live

1. **Run manually, by hand, against a small known list first** (e.g. the existing
   enforcement-orders bank list) before widening to the full ~4,500-institution universe.
2. **Force a fake diff** to confirm the email-sending path actually works end to end.
3. **Historical validation using SVB (Silicon Valley Bank) as a test case:** run the ratio
   logic against SVB's known pre-failure quarters (through Q4 2022 — SVB failed mid-Q1 2023,
   so there is no Q1 2023 Call Report to expect). Confirm the AFS/HTM securities
   deterioration and capital pressure show up as Tier 1/Tier 2 triggers in the quarters
   leading up to failure. Confirm you're scoped to the bank subsidiary's RSSD (Silicon
   Valley Bank), not the holding company (SVB Financial Group) — same entity-scoping
   distinction that came up with Banc of California.
4. Only widen to the full institution universe once the small-list test is trusted.

## 11. Known limitations / things not to forget

- **90-day JWT token expiry, manual renewal, no auto-refresh** — put a calendar reminder on
  this separately from the code; the code should fail loudly (see §1) as a backstop, not be
  relied on as the only safeguard.
- This spec covers **live FFIEC data only** — it does not touch FreeNIC or any third-party
  dataset. No licensing considerations apply to this pipeline.
- Materiality floor (§6, Tier 3) is a starting point, not final — expect to tune it after
  seeing a week or two of real output; too tight and Tier 1/2 get noisy, too loose and real
  restatements get buried.
