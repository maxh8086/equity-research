# Kite-ATH-Scanner fixtures: real data, taken from a working system

Unlike every other fixture directory here, these rows are **real** — not
hand-written to a documented shape. They were extracted on 2026-09-29 from the
on-disk caches of a separate, working project (`Kite-ATH-Scanner`, same author,
not a dependency of this repo).

That project is not an upstream we track and its caches are not reproducible
from here, so the method that produced them is recorded below. Without it these
files are numbers with no provenance, which is the one thing CLAUDE.md R2 will
not allow.

**None of this may reach a knowledge store.** It is fixture and evidence data
only. The reasons are in "Why none of it is store data" at the bottom, and they
are not stylistic.

## What was taken

| File | Rows | Extracted from |
|---|---|---|
| `candles_corporate_action_windows.csv` | 103 | `data/history.db`, table `candles` (2,629,487 rows, 750 symbols, 2000-01-03 … 2026-09-25) |
| `nse_announcement_categories.csv` | 163 | `data/factors.db`, table `announcements` (391,074 rows, 2,468 ISINs, 2024-05-29 … 2026-09-29) |
| `nse_announcements_sample.json` | 60 | the same `announcements` table, 10 rows from each of 6 categories, oldest first |

## How the source caches were built

### `history.db` — Upstox daily candles

`scanner/upstox.py` + `scanner/history.py`. Upstox API v3 over HTTPS with a
long-lived *analytics* access token (data-only; it cannot place orders), read
from the environment. Instruments addressed as `NSE_EQ|{ISIN}` — ISIN-native,
so a tradingsymbol rename cannot break a lookup. Histories fetched in chunks
walking backwards from today, stopping at the first empty chunk, and cached
per-symbol in SQLite so a repeat run fetches only days after the last cached
date.

Two limits in that module were **verified against the live API on 2026-09-28**,
not read from the docs:

- a 3,653-day (10y + 1d) daily request succeeds, while `2000-01-01 … 2026-09-28`
  is rejected with `UDAPI1148 Invalid date range` — hence `MAX_DAYS_PER_CALL = 3650`;
- requesting `1996-09-28 … 2006-09-27` returns candles starting 2000-01-03, so
  nothing earlier than 2000 exists to fetch — hence `HISTORY_FLOOR = 2000-01-01`.

RELIANCE's full history is 6,648 candles in 3 requests. Rate limiting and 429
backoff live in the client only, never also in the caching layer, on the stated
grounds that throttling in two places compounds silently.

Both limits agree with what `ingest/upstox/parser.py` already assumes. They are
recorded here because they are *observations* and the parser's are citations.

### `factors.db` — NSE corporate announcements

`scanner/factors.py`, `fetch_announcements()`. Plain `requests` against
`https://www.nseindia.com/api/corporate-announcements` with
`index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY`, sliced into calendar
months so each request stays bounded and a partial failure costs one month.
`time.sleep(1)` between chunks, including on the failure path. Rows trimmed to
the fields the classifiers use before accumulating, because keeping NSE's full
metadata for a multi-month window runs to hundreds of megabytes.

**The method is not adoptable, and the reason matters.** That fetch sends a
`Chrome/122` `User-Agent`, commented in the source as "Browser-like headers
required by NSE". That is browser impersonation — forbidden by CLAUDE.md
"Breakage", and already rejected once in `docs/mcp-repo-review.md`. The rows it
produced are genuine NSE data and are fine to keep as fixtures; the way they
were fetched must not be copied into an `ingest/` adapter. Our route to the same
data is the official archive, or the drop folder.

The single-request-per-month shape and the 1-second spacing *are* worth copying.
The header is not.

## What the fixtures are evidence of

### 1. Upstox serves a back-adjusted history (`candles_corporate_action_windows.csv`)

`ingest/upstox/adapters.py` sets `as_of` to the fetch time rather than the trade
date because "a vendor history can be revised after the fact (adjusted for a
later split or bonus)". That was an assumption. These windows measure it.

At a known ex-date an unadjusted close-to-close series must step by the action's
ratio. Across five known NSE capital actions, it never does:

| Symbol | Ex-date | Action | Prev close | Ex close | Ratio |
|---|---|---|---|---|---|
| RELIANCE | 2017-09-07 | 1:1 bonus | 392.10 | 389.90 | 0.994 |
| INFY | 2018-09-11 | 1:1 bonus | 730.85 | 734.30 | 1.005 |
| INFY | 2015-06-15 | 1:1 bonus | 493.77 | 495.23 | 1.003 |
| TITAN | 2011-09-05 | 1:10 split | 210.90 | 219.70 | 1.042 |
| TCS | 2018-05-31 | 1:1 bonus | 1757.05 | 1741.05 | 0.991 |

A 1:1 bonus would show ≈0.5 and a 1:10 split ≈0.1. Five of five show no step:
the vendor already divided every earlier price. The last three rows of the CSV
are RELIANCE's first cached days — a 2000-01-03 close of 23.10 against an
as-traded figure around ₹250, a retroactive adjustment of roughly 10.8×.

This is exactly the R2 failure the repo guards against: a series read at `t`
that already knows about corporate actions after `t`. It is the empirical
justification for `core/compute/adjustment.py` computing factors from
`corporate_action` rows filtered by `price_adjustments_as_of`, instead of
trusting the vendor's adjusted series.

### 2. NSE's `desc` is a controlled vocabulary (`nse_announcement_categories.csv`)

Every distinct `desc` value across 391,074 announcements, with its count. There
are **163** of them — the source project's docstring says 132, so the vocabulary
has grown, which is itself a reason to keep the measured list rather than a
remembered number.

`desc` is chosen from a dropdown, not typed. Matching the category exactly is
far more reliable than guessing at phrasing, which bears directly on
`core/compute/order_terms.py`: a category tier should come before any phrase
tier, and phrases should only ever be matched against attachment text.

Counts that make the case concrete:

| Category | Rows |
|---|---|
| `BAGGING/RECEIVING OF ORDERS/CONTRACTS` | 2,360 |
| `AWARDING OF ORDER(S)/CONTRACT(S)` | 445 |
| `ACTION(S) TAKEN OR ORDERS PASSED` | 2,262 |
| `CAPACITY ADDITION` | 459 |
| `COMMENCEMENT OF COMMERCIAL PRODUCTION/OPERATIONS` | 458 |
| `TRADING WINDOW` | 22,004 |

The third row is the point. It contains the word "orders" and means a regulator
or court acted *against* the company — the opposite signal — and there are very
nearly as many of them as there are genuine order wins. An exclusion set is not
a refinement here; without it the category is roughly half noise of inverted
sign.

`TRADING WINDOW` is listed because of a recorded false positive: matching
order-win phrases against `desc` rather than attachment text made "Trading
Window" register as an order win via "WIN", across 22,004 rows. Two further
measured negatives from the same project: bare `CAPACITY` and `COMMISSION` as
expansion keywords fired on 526 and 433 filings respectively with no expansion
content, matching "capacity utilisation", "in his capacity as", "Commissioner"
and commission payments.

### 3. Real announcement row shape (`nse_announcements_sample.json`)

60 rows, 10 from each of the six categories above, in the field shape NSE
returns: `sm_isin`, `symbol`, `an_dt`, `desc`, `attchmntText`, `sort_date`,
`seqId`, `attchmntFile`, `attFileSize` (stored under the snake_case column
names the source table uses).

Two field notes from the source project, both load-bearing for any parser we
write: `attchmntText` is only a truncated snippet — median 100 characters, and
just 9.5% carry a money figure — so an order's value is not in it. The value is
in the PDF at `attchmntFile`, and `attFileSize` exists so a fetch can skip an
outsized file before downloading it. All 391,074 rows carry an `attchmntFile`.

## Why none of it is store data

- **The candles cannot be un-adjusted.** The as-traded prices are not
  recoverable from them, and the adjustment they carry uses factors known in
  September 2026. Loading them into a price store would import look-ahead bias
  into every date before the last corporate action.
- **The announcements have one fetch time for the whole table.** `factors.db`'s
  `fetch_metadata` holds a single row: `announcements, 2024-05-29, 2026-09-29,
  fetched_at 2026-09-29T00:26:33`. An honest `as_of` for every row is therefore
  that one timestamp, which is true but useless as history — a 2024 filing did
  not become knowable in 2026. `an_dt` is event time, not `as_of`.
- **No `content_hash`, no `source_url` per row, no `extracted_by`.** The raw
  bytes were not kept; only trimmed fields were.
- **The fetch used browser impersonation**, so the rows could not be
  re-collected the same way even if the columns were sufficient.

Use them to exercise parsers and computations offline. Get store data from
`ingest/`.

## Reproducing

The source caches live outside this repo and are not committed (they are 244 MB
and 158 MB). The extraction was three SQL selects; the window bounds, the six
categories and the ten-row limit are all stated above, so the same files can be
regenerated from an equivalent cache.
