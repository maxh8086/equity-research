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

## Layout

| Path | What |
|---|---|
| `core/compute/` | Pure functions. No I/O. Property-tested. |
| `core/compute/membership.py` | Index membership intervals (member / uncertain) from dated constituent lists |
| `core/db/base.py` | Provenance mixin: `as_of`, `content_hash`, `source_url`, `extracted_by`, `model_version`, `ingested_at` |
| `core/db/models.py` | Store ① `financial_facts`; `raw_source_file`; `entity` / `entity_isin`; `index_snapshot`, its constituents and quarantine |
| `ingest/nse_indices/` | Nifty 50 / Next 50 constituent lists: NSE archive, drop folder, Wayback captures; one parser |
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
