# Indian Equity Knowledge System

Point-in-time knowledge system for NSE/BSE equity research. Rules and scope
live in [CLAUDE.md](CLAUDE.md); build order in
[getting-started-plan.md](getting-started-plan.md).

## Setup (Windows / PowerShell)

```powershell
py -3.13 -m venv .venv          # CLAUDE.md targets 3.12; any >=3.12 works
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # exact pinned rebuild
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
docker compose up -d --wait db   # Postgres 16 + pgvector on 127.0.0.1:5433
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m pytest -x --tb=short
```

Tests recreate the `equity_test` database from scratch and run migrations
up → down → up on every run. Pure tests (`core/compute`) need no database:
`pytest -m "not db"`.

## Layout

| Path | What |
|---|---|
| `core/compute/` | Pure functions. No I/O. Property-tested. |
| `core/db/base.py` | Provenance mixin: `as_of`, `content_hash`, `source_url`, `extracted_by`, `model_version`, `ingested_at` |
| `core/db/models.py` | Store ① `financial_facts` |
| `core/db/pit.py` | Point-in-time reads — `as_of` is a required argument |
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
