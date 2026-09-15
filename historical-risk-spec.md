# Historical Enforcement & Failure-Risk Analysis — Build Spec

**Purpose:** Pull UBPR ratio history back to 2012 via FFIEC's public bulk data (no API
token needed), join it against the existing enforcement orders table, and run a
**descriptive field-stats analysis** of which ratios move around banks becoming LTWC
(Less Than Well Capitalized) or heading toward failure — plus simple survival curves on
how long banks stay under different order types.

**Explicit non-goal:** this is not a replication of Correia/Luck/Verner's "Failing Banks"
methodology. No matched control group, no causal claims, no Cox regression. If a future
phase wants that level of rigor, it's a separate, much larger project — see §7.

---

## 1. Data pull — bulk, not the rate-limited API

- **Use `ffiec_data_collector`** (github.com/call-report/data-collector), which talks
  directly to the CDR's public bulk-download backend (`cdr.ffiec.gov/public/PWS/
  DownloadBulkData.aspx`) via direct HTTP — no Selenium, no browser automation.
- **Product: `UBPR_RATIO_FOUR`** (four quarters per pull) — the computed ratios
  themselves, not raw Call Report schedules. **Range: 2012 Q1 through the most recently
  filed quarter** — as of this spec being written (Sep 2026), that's **Q2 2026
  (06/30/2026)**, since Q3 2026 isn't due until ~45 days after quarter-end (mid-November
  2026) and won't be in the bulk product yet. Don't hardcode "2026 Q2" as the end date in
  the actual script — pull whatever the latest available period is at run time, so this
  doesn't need a manual update every quarter.
- **No FFIEC PWS account or JWT token required for this pull.** That auth system is
  specific to the rate-limited per-institution REST API (used by the separate daily
  monitor project, see `ffiec-monitor-spec.md`) — it does not gate the bulk downloads.
  This removes the 90-day token-renewal maintenance burden entirely for this project.
- **Sanity-check file size on one quarter before looping all of them** — confirm
  `UBPR_RATIO_FOUR` is meaningfully smaller than the ~500MB/quarter full Call Report
  product before assuming either size estimate.
- **Be a reasonable citizen of a public government server** — no need to parallelize
  aggressively; sequential quarterly pulls are fine and won't strain anything.

## 2. Survivorship bias — must be handled, not optional

The bank universe changes constantly — failures, mergers, charter conversions. Pulling
only banks that exist *today* silently drops the institutions most likely to have had the
worst enforcement histories.

- Cross-reference against **FDIC BankFind/SDI's historical institution list**, which
  includes failed and acquired institutions, not just active ones.
- Every bank that ever had an active RSSD in the 2012-present window belongs in the pull,
  regardless of whether it still exists.
- Keep a `status` field per bank (active / acquired / failed, with date) — this is needed
  both for the survivorship check and for the survival-curve work in §5.

## 3. Ratios that flag failure/LTWC risk — the actual analytical core

This merges the existing 12-ratio cheat sheet with the PCA/LTWC thresholds and what the
literature (§7) independently confirms matters most. Treat this table as the spec for
which fields to pull and threshold against, not just background.

| Ratio | LTWC / PCA threshold | Why it matters (literature backing) |
|---|---|---|
| Tier 1 Leverage Ratio | < 5% (well-cap) / <4% / <3% (escalating PCA tiers) | Top predictor in Hu, Shao & Zhang (2025) using FDIC data 2001-2023 |
| Tier 1 Risk-Based Capital Ratio | < 8% | Standard PCA input |
| Total Risk-Based Capital Ratio | < 10% | Second independently-confirmed top predictor (same paper) |
| CET1 Ratio | < 6.5% | Standard PCA input, post-Basel III |
| ROAA | Negative, or <0.50% for 2+ consecutive quarters | Correia/Luck/Verner and Hu/Shao/Zhang both flag earnings deterioration as an early signal |
| Net Interest Margin | Compression trend, not a hard cutoff | Early-warning signal, precedes capital deterioration |
| Noncurrent Loans+OREO Ratio | Doubling QoQ, or crossing ~3% absolute | Asset-quality deterioration signal |
| Efficiency Ratio | >10pt QoQ increase | Cost-structure breakdown, visible before capital impact (see Stellar Bank case) |
| Net Loans & Leases to Deposits | Rapid increase | Funding-stress signal |
| Net Non-Core Funding Dependence | Elevated / rising | Liquidity fragility |
| Charge-off Ratio vs. Peer | Materially above peer | Asset-quality relative signal |
| Loan Growth vs. Capital Growth | Loan growth outpacing capital growth | Leverage build-up signal |

**CAMELS severity as a feature, not just a label:** per "Supervising Failing Banks"
(NY Fed SR 1168), a CAMELS-5 rated bank is ~80x more likely to fail than a CAMELS-1 —
if CAMELS composite ratings are obtainable for your tracked banks, treat rating as an
ordered variable alongside the ratios above, not a separate side note.

## 4. Field stats to actually produce (descriptive, not causal)

- **Frequency counts:** orders per year, by type (S&S / AML / CMP), by regulator, 2012-present
- **Before/after ratio snapshots:** for each bank with an order, show the ratio table
  (§3) at T-4, T-1, T (order effective quarter), T+4 — no control group needed, this is
  simple description of what the numbers looked like around the event
- **Watch List cross-section:** what share of currently-LTWC or currently-ordered banks
  are on IntraFi's own network (ties back to the original placement-data angle, using
  only public data here — see note in §8 on keeping that boundary clean)
- **Order-type × ratio cross-tab:** e.g., do AML orders cluster around different ratio
  profiles than S&S orders, purely descriptively (means/medians by group, no regression)

## 5. Survival analysis — the one inferential piece worth including

- **Kaplan-Meier curves only, not Cox regression** (Cox is a modeling-phase task, not
  this field-stats pass — see §7).
- **Inputs needed:** order start date, order end date (or "still active" = censored),
  order type. All three already exist in the enforcement orders table — **no ratio data
  needed for this specific piece.**
- **R packages:** `survival` (core) + `survminer` (plotting) — both well-documented,
  standard for exactly this use case.
- **Output:** "what fraction of banks are still under an active order after 1/2/4 years,"
  split by order type — a genuinely useful, non-obvious finding a simple average would hide.
- Handle censoring properly: an order still open today is not "cured at present," it's
  censored — don't drop these banks or treat them as cured.

## 6. Build approach

- **Do this directly in R**, not as a formalized skill, for this first pass — the goal is
  the finding, not a reusable pipeline yet.
- **Only write a `SKILL.md`** capturing the method (censoring handling, package choices,
  plot conventions) **after** the analysis has run once and proven useful — documenting a
  process before validating it is speculation, not documentation.
- Store everything in the existing project DuckDB, joined on RSSD ID against the
  enforcement orders table already in use.

## 7. Explicit scope boundary — what this project does NOT do

- No matched comparison group of non-ordered banks
- No event-study regression design
- No Cox proportional hazards / covariate-adjusted survival modeling
- No attempt to reproduce Correia/Luck/Verner's methodology at their level of rigor

These are all legitimate next steps for a later, larger modeling phase (see the existing
`learning-roadmap.md` — tidymodels, `logistf`, time-based splits) — not this pass. Going
further here risks quietly redoing the "Failing Banks" study with a fraction of its rigor;
the value of this phase is a fast, honest, descriptive pass, not a shadow replication.

## 8. One boundary to keep clean

This analysis uses **only public data** — FFIEC bulk ratios, FDIC failure/SDI records,
the enforcement orders table already built from public regulator filings. If a Watch
List/network cross-section (§4) is ever extended to actually use IntraFi's own
subscription-walled placement data, that is a separate decision requiring the same
data-governance conversation already had for that data source — not something to fold
into this pull casually.

## 9. Portability checklist — before zipping this script to send to work

This project's script gets built locally, then moved to a fresh, work-side DuckDB (see
decision: rebuild clean at work rather than reuse the personal desktop DuckDB, which has
FreeNIC data mixed into it). Since the script itself is being developed alongside that
FreeNIC work, check all three before zipping and sending:

- **No FreeNIC references.** Grep for `freenic`, `data.freenic.org`, `freenic_pilot`, or
  any table/variable names from that exploration. This script should only reference
  `ffiec_data_collector`, the public CDR bulk endpoints, and the enforcement orders table.
- **No hardcoded local paths.** The DuckDB file path should be a config value or
  environment variable, not a literal path to anything on the personal desktop — this
  also just breaks less when it lands somewhere else.
- **No baked-in credentials.** This pull doesn't need the FFIEC PWS token at all (§1), so
  there shouldn't be any secrets in this script — but double-check nothing got copied over
  from the daily-monitor project's connection code.

**Quick way to confirm all three at once:** ask Claude Code directly, "does this script
reference any local file paths, credentials, or FreeNIC-related sources? List everything
it touches" — a clean answer is the final check before the zip leaves the machine.

## 10. Cleanup script — logical names, only the columns needed

The raw bulk pull (§1) returns every UBPR field the product exposes, under FFIEC's own
field IDs (e.g. `UBPRE...`-style codes) — not something you want to carry into analysis
as-is. Build a **separate cleanup step, run right after the pull, before anything touches
the ratios table:**

- **A field-mapping config file** (CSV or YAML, not inline code) — one row per field:
  `raw_field_id`, `logical_name`, `ubpr_page` (cross-referenced to §3's ratio table),
  `active_from`, `active_to` (nullable). This is a real artifact, checked once against the
  UBPR User's Guide (versioned — cdr.ffiec.gov hosts different guide versions, e.g. v175,
  v181; confirm which version's field list matches which years of your pull), not
  something to leave as inline renaming logic buried in a script.
- **Select only the ~12-15 fields in §3's ratio table, plus identifiers** (RSSD ID,
  period end, bank name, peer group ID) — not the full UBPR field set. Everything else
  gets dropped at this step, not carried downstream to be filtered out later.
- **Output a clean, logically-named table** — `leverage_ratio`, `total_rbc_ratio`,
  `roaa`, etc. — that the rest of the project (field stats, survival analysis) reads
  from. Nobody downstream should ever need to look up a raw UBPR field code again.

## 11. Handling Call Report/UBPR definitional changes since 2012

This is real, not a minor caveat — the UBPR itself has changed multiple times in this
window, per FFIEC's own published changelog
([ffiec.gov/data/ubpr/latest-updates](https://www.ffiec.gov/data/ubpr/latest-updates)),
which should be checked directly rather than assumed complete from this list:

| When | What changed | Which ratios affected |
|---|---|---|
| 2015-2019 (Basel III phase-in) | CET1 introduced as a new capital measure; Tier 1/Total capital definitions adjusted | CET1 Ratio doesn't exist as a field before ~2015 — don't expect it, don't zero-fill it |
| 2020-2023 (CECL adoption, phased by bank size) | Allowance for Credit Losses (ACL) replaces Allowance for Loan and Lease Losses (ALLL) | Reserve/noncurrent-adjacent ratios; adoption date varies by institution size, not one clean cutover for the whole universe |
| March 2024 | UBPR CECL nomenclature & loan modification changes | Old TDR line items were deliberately kept ~5 years post-transition specifically for historical comparability, then cease — FFIEC's own accommodation for exactly this problem |
| ~2021 | FDIC brokered deposit rule revision — reciprocal/brokered deposit classification changed | Net Non-Core Funding Dependence, brokered deposit ratios — worth extra care given IntraFi's own business is reciprocal deposits |
| Jan/Feb/July 2026 | UBPR Liquidity pages, peer groups, and titles all revised | Liquidity-related ratios, and peer group averages for banks under $300M — right in the middle of this pull's window |

**Practical rule this drives:** before treating any ratio's sharp before/after move
around a known regime-change date as a *risk signal* in the field stats (§4), check
whether it's actually a *definitional break* instead. A jump in a reserve ratio in 2020-
2023 is a strong candidate for "this bank adopted CECL," not "this bank's credit quality
changed" — the field-mapping table's `active_from`/`active_to` columns exist specifically
so this check can be automated rather than caught by eye.

**This list is a starting point, not exhaustive** — the FFIEC changelog page should be
checked directly as part of building the field-mapping config, since new entries get
added there over time (most recently July 2026, per the table above).

