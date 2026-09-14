# Temporal model

How every store handles time. R2 in CLAUDE.md is the rule. This document
explains how the rule is applied. Read it before building any store.

Status: **implemented** for `financial_facts`. Everything else is the agreed
design for stores not yet built.

## Three times

| Time | Meaning | Examples |
|---|---|---|
| **Event time** | When it happened in the world, or which period it covers | `period_end`, rating action date, ex-date, `valid_from`/`valid_to` |
| **`as_of`** | When the public could first have known it | Exchange dissemination timestamp of the filing |
| **`recorded_at`** | When *this system* stored or derived it (`ingested_at` on parsed stores) | Wall clock when the job ran |

- **Knowledge vs. selection:** every read filters `as_of <= t`. Event time only
  selects *which* facts; knowledge time decides whether they are *visible*.
- **Date-only sources:** set `as_of` to 23:59:59 IST on that date. A signal may
  appear later than reality, never earlier.
- **Historical filings are not backfills:** loading a 2019 filing today stores
  its 2019 publication time in `as_of` and today in `recorded_at`. R2 forbids
  inventing knowledge times for things the system derived.
- **Per-store sanity checks:** `financial_facts` requires `as_of` (IST date) >
  `period_end` (implemented). Corporate actions require `as_of` <= ex-date.
  Guidance claims have no such check, because they describe future periods.

## Nothing is updated

Every store is append-only, enforced by DB triggers that reject
UPDATE/DELETE/TRUNCATE. A change is a new row with a later `as_of`. The current
state at `t` is the latest row with `as_of <= t`.

## Three timeline shapes

**1. Point events** — `company_event`, `rating_action`, `order_win`
- One row per event: event time, `as_of`, `evidence_url`.
- Severity is computed by code and stored with `rule_version`. A rule change
  writes new rows; old severities are never recomputed in place.

**2. Lifecycles** — `guidance_claim`, `order_status`
- The claim row is frozen: verbatim quote, `hedge_strength`, target metric and
  period, `as_of` = concall date.
- Status transitions go in a separate append-only table:
  `OPEN → MET | MISSED | PARTIAL | SILENT`. Each row stores `observed_at`, the
  ids of the exact `financial_facts` versions used as evidence, and `rule_version`.
- `SILENT` counts *filings*, not days: two consecutive filings (ordered by
  `as_of`) after the claim with no mention.
- If an evidence fact is later restated, the old verdict stands. A new status
  row is appended, flagged `evidence_restated`.

**3. Intervals** — `force_intensity`, rating outlooks
- `valid_from`, `valid_to` (NULL = open).
- Closing an interval appends a new version with `valid_to` set and a later
  `as_of`. A read at `t` sees the interval as it was understood at `t`.
- `first_detected_on` is the real wall-clock time of live detection. It is
  never set earlier.

## Hypotheses (R3)

- A counter-thesis is stored as a structured condition (metric, operator,
  threshold, `observe_on`) plus a hash of that condition. It cannot be
  reworded later.
- On `observe_on`, deterministic code evaluates it using only data with
  `as_of <= observe_on`, and records the verdict with the fact versions used.
- The model never sees the outcome before the verdict is written, and never
  re-evaluates it.

## Live vs. replay

- **Live tables** record what the system actually knew and detected, on the
  real clock.
- **Replays/backtests** run the same functions with an explicit `t`. Every
  function takes `t` and reads only `as_of <= t`. Output goes to a separate
  replay schema tagged with `run_id`, code version and data snapshot hash.
- Replay output is **never** written to live tables. This makes backfilling
  `first_detected_on` impossible by construction.

## Enforcement

| Mechanism | State |
|---|---|
| Append-only triggers and `as_of` sanity checks per store | Done for `financial_facts` |
| One point-in-time read function per store, `t` required (`core/db/pit.py`) | Done for `financial_facts` |
| Naive datetimes rejected before reaching the DB (`core/timezones.py`) | Done |
| Architecture test: no direct queries against store tables outside PIT functions (`tests/test_architecture.py`) | Done |
| Derived stores add `rule_version`, evidence links, `recorded_at` | Added as each store is built |
