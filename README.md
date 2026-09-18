# Indian Equity Knowledge System

Point-in-time knowledge system for NSE/BSE equity research. Rules and scope
live in [CLAUDE.md](CLAUDE.md); build order in
[getting-started-plan.md](getting-started-plan.md).

## Setup (Windows / PowerShell)

```powershell
py -3.13 -m venv .venv          # CLAUDE.md targets 3.12; any >=3.12 works
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # exact pinned rebuild
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
docker compose up -d --wait db blob   # Postgres 16 + pgvector on :5433, MinIO on :9000
docker compose run --rm blob-init     # write-once buckets (object lock); safe to re-run
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m pytest -x --tb=short
```

Tests recreate the `equity_test` database from scratch and run migrations
up → down → up on every run. Tests that need no services:
`pytest -m "not db and not blob"`.

## Ingest adapters

```powershell
.\.venv\Scripts\python.exe -m ingest list            # adapters, source class, stores, switch state
.\.venv\Scripts\python.exe -m ingest run NAME        # exit 0 = succeeded or disabled
.\.venv\Scripts\python.exe -m ingest canary          # daily shape check; disabled adapters skipped
.\.venv\Scripts\python.exe -m ingest reparse NAME    # re-derive from stored bytes; never re-fetches
```

Every adapter is off until `EQUITY_SOURCE_<NAME>_ENABLED=true`; `web_scrape`
adapters also need `EQUITY_WEB_SCRAPING_ENABLED=true`. With
`EQUITY_DEPLOYMENT_MODE=commercial`, any scraping switch refuses startup, and
so does a switch that names no adapter.

Raw bytes go to blob storage (keyed by SHA-256, write-once) before parsing;
`raw_source_file` records source URL, publication time (`as_of`) and fetch
time. Files downloaded by hand go in `EQUITY_DROP_FOLDER/<adapter name>/`,
each with a `<file>.meta.json` sidecar:

```json
{"source_url": "https://…", "published_at": "2024-03-01T23:59:59+05:30", "media_type": "text/csv"}
```

Index constituent lists (`nse_indices_constituents_drop`): the index comes from
the file name in `source_url` (`ind_nifty50list.csv`, `ind_niftynext50list.csv`).
For a Wayback copy, `source_url` is the capture URL and `published_at` its
capture time. Rows that fail checks land in `index_snapshot_quarantine`; review
them with `core.db.pit.index_quarantine_review_as_of`, which marks a rejected
file (or a row belonging to a since-superseded snapshot) superseded once the
same file has been loaded again -- typically via `python -m ingest reparse`
after a parser rule change, which re-derives from the bytes already in blob
storage rather than re-fetching.

Bhavcopy (`nse_bhavcopy`, `nse_bhavcopy_drop`): NSE's daily UDiFF CSV, one
zipped file per trade date, walked day by day from its format's own start
(2024-07-08) to today; a 404 means "not a trading day" and is skipped, not a
block. Only confirmed equity series (EQ, BE) are stored, as `nse_bhavcopy_row`
-- both the cross-check for Upstox prices and, via
`core.db.pit.symbol_to_isin_as_of`, the dated ticker -> ISIN map. Rows that
fail checks land in `nse_bhavcopy_quarantine`, reviewed the same way via
`core.db.pit.bhavcopy_quarantine_review_as_of`.

Upstox daily candles (`upstox_daily_candles`, `upstox_daily_candles_drop`): the
v3 historical-candle API, one ISIN at a time (`NSE_EQ|<ISIN>`) for every ISIN
in any Nifty 50 / Next 50 list known, in fixed decade windows from 2000-01-01.
Needs `EQUITY_UPSTOX_ACCESS_TOKEN` from a human login (tokens expire daily); a
401 stops the run. Candles are stored as `upstox_candle` with `as_of` = **fetch
time**, not the trade date: a vendor history may be adjusted after the fact, so
it is only known to be the vendor's view from when it arrived. A day's candle is
not requested before 18:00 IST that day. `core.db.pit.upstox_bhavcopy_crosscheck_as_of`
compares them with the exchange's own bhavcopy rows. Built against the
documented response shape until an API app exists
(`tests/fixtures/upstox/SOURCES.md`). For the drop folder, save the raw response
body with the request URL as `source_url` and the download time as
`published_at`. Review quarantine with
`core.db.pit.upstox_quarantine_review_as_of`, which judges entries against the
current rule version.

Corporate actions (`nse_corporate_actions_drop`, `corporate_actions_curated_drop`),
drop folders only; switches `EQUITY_SOURCE_NSE_CORPORATE_ACTIONS_DROP_ENABLED`
and `EQUITY_SOURCE_CORPORATE_ACTIONS_CURATED_DROP_ENABLED`:
- **NSE export:** the corporate-actions CSV from nseindia.com, saved by hand,
  with the page as `source_url` and the download time as `published_at`.
  Splits, consolidations, bonuses, rights and dividends are read from the
  PURPOSE text by fixed patterns; anything else named but unreadable, and
  every demerger (its ratio is not in an exchange field), goes to
  `corporate_action_quarantine`. Symbols resolve to ISINs through the
  bhavcopy map, so load bhavcopy first; a rerun retries unresolved symbols.
- **Curated file:** a CSV with exactly the columns `action_key, isin,
  action_type, status, announced_at, ex_date, record_date, face_value_from,
  face_value_to, shares_new, shares_held, issue_price, dividend_per_share,
  retained_fraction, new_isin, evidence_url, verified_by, note`. One row per
  action version, typed from the evidence by a named person (`verified_by`),
  with an aware `announced_at`. Use it for demergers, ISIN changes and
  corrections; a later row with the same `action_key` supersedes an earlier
  one (e.g. `status=withdrawn`).

`core.db.pit.price_adjustments_as_of` returns the factors known at `t` and
every action it skipped, with the reason; `adjusted_closes_as_of` applies them
to bhavcopy closes across the ISIN lineage. Review quarantine with
`core.db.pit.corporate_action_quarantine_review_as_of`.

XBRL results (`nse_xbrl_results_drop`), drop folder only; switch
`EQUITY_SOURCE_NSE_XBRL_RESULTS_DROP_ENABLED`. Save a results XBRL file from
`https://nsearchives.nseindia.com/corporate/xbrl/<file>` byte-for-byte, with
that URL as `source_url` and NSE's dissemination time (the listing's
`exchdisstime`, IST) as `published_at`. The parser (`ingest/nse_xbrl`)
supports the SEBI 2020 Ind AS and NBFC taxonomies, the 2019 banking taxonomy,
and the 2020 life and general insurance taxonomies. Older filings are quarantined
as `unsupported_taxonomy`. Elements map to line items through a hardcoded
table (`mapping.py`, `RULE_VERSION`). An element missing from the table,
an unexpected unit, a malformed value, or one element tagged twice with
different values is quarantined, never guessed. Every non-dimensional context
is stored: the quarter and the year to date. Segment and other dimensional
facts are counted on `financial_filing` and deferred. The ISIN comes from
the file (banks, insurers) or from the symbol, through bhavcopy rows from the
10 days before publication and index lists from the 190 days before it. Every
read must agree. Load bhavcopy or index lists first; a rerun retries an
unresolved file. Review quarantine with
`core.db.pit.financial_facts_quarantine_review_as_of`.

Shareholding patterns (`nse_shareholding_drop`), drop folder only; switch
`EQUITY_SOURCE_NSE_SHAREHOLDING_DROP_ENABLED`. Save a shareholding-pattern XBRL
file from `https://nsearchives.nseindia.com/corporate/xbrl/<file>`
byte-for-byte, with that URL as `source_url` and NSE's broadcast time (IST) as
`published_at`. `published_at` must fall after the "as on" date and not before
the timestamp in the file name (a 12-hour clock, read as AM), or the file is
quarantined as `implausible_as_of`. The parser (`ingest/nse_shp`) supports the
2020-09-30, 2025-05-31 and 2025-10-31 taxonomies. It stores share counts, not
percentages, per shareholder category and measure (holders, shares, voting
rights, locked-in, pledged and other encumbered shares, demat). Every parent
category must equal the sum of its children, or the whole file is rejected as
`totals_mismatch`. Named-holder facts are counted and deferred. A revised
filing is another file with a later `as_of`; `core.db.pit.shareholding_pattern_as_of`
returns the newest filing known at `t` for each "as on" date. The ISIN is
resolved as for results files. Review quarantine with
`core.db.pit.shareholding_quarantine_review_as_of`.

## Layout

| Path | What |
|---|---|
| `core/compute/` | Pure functions. No I/O. Property-tested. |
| `core/compute/membership.py` | Index membership intervals (member / uncertain) from dated constituent lists |
| `core/compute/price_crosscheck.py` | Vendor daily bars vs. the exchange's record of the same days |
| `core/compute/adjustment.py` | Price adjustment factors for splits, bonuses, rights and demergers, and their application |
| `core/db/base.py` | Provenance mixin: `as_of`, `content_hash`, `source_url`, `extracted_by`, `model_version`, `ingested_at` |
| `core/db/models.py` | Store ① `financial_facts`; `raw_source_file`; `entity` / `entity_isin`; `index_snapshot`, its constituents and quarantine; `nse_bhavcopy_row` and its quarantine; `upstox_candle` and its quarantine; `corporate_action` and its quarantine |
| `ingest/nse_indices/` | Nifty 50 / Next 50 constituent lists: NSE archive, drop folder, Wayback captures; one parser |
| `ingest/nse_bhavcopy/` | Daily NSE bhavcopy: archive host and drop folder; one parser |
| `ingest/upstox/` | Upstox v3 daily candles by ISIN: API and drop folder; one parser |
| `ingest/corporate_actions/` | Corporate actions: NSE export and curated file, both drop folders |
| `ingest/nse_xbrl/` | XBRL results files → `financial_filing`, `financial_facts`: hardcoded element mapping, drop folder |
| `ingest/nse_shp/` | Shareholding-pattern XBRL files → `shareholding_filing`, `shareholding_pattern`: hardcoded category mapping, drop folder |
| `core/db/pit.py` | Point-in-time reads — `as_of` is a required argument |
| `core/blob.py` | The one blob-storage interface (S3-compatible now; Azure later) |
| `core/sources.py` | Source classes and deployment modes |
| `ingest/base.py` | Adapter base: declarations, switches, `store_raw`, drop folder |
| `ingest/http.py` | Throttled, identified HTTP; stops on a block, never escalates |
| `ingest/registry.py` | Adapter discovery, `extracted_by` → source class, startup checks |
| `migrations/` | Alembic. Every schema change is a migration; a test fails if models and migrations drift. |

## Invariants enforced by the database, not by convention

`financial_facts`:
- **Append-only** — triggers reject UPDATE, DELETE and TRUNCATE. Restatements are new rows with a later `as_of`.
- **No look-ahead** — `as_of` (in IST) must be after `period_end`.
- **No model writes** — `model_version` must be NULL (R1).
- Indian ISIN format; SHA-256 `content_hash`; `xbrl_element` present iff `fact_kind = 'reported'`.
- Money is `NUMERIC(28,6)` ↔ `Decimal`; naive datetimes are rejected before they reach the DB.

`as_of` means *when the information became public* (filing dissemination
time), not when we ingested it — that is `ingested_at`.
