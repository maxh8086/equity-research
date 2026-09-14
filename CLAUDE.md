# Indian Equity Knowledge System

A point-in-time knowledge system for NSE/BSE equity research. Long-horizon
buy-and-hold (>12 months), family use only.

## Non-negotiable rules

These are not style preferences. Violating them corrupts data that cannot be
repaired later. If a request conflicts with one of these, stop and say so.

### R1 — Deterministic where computable
Code computes; LLMs narrate. Never write code that asks a model to:
- perform arithmetic (ratios, WACC, DCF, growth rates, percentages)
- extract a number that exists in XBRL
- decide whether a threshold was crossed
- assign a macro-to-sector mapping

LLMs are only for: narrative extraction (MD&A, concall Q&A), assumption
framing, and prose generation. Every number in the system must trace to a
parser or a computation.

### R2 — as_of on everything, never backfilled
Every table carries `as_of`, `content_hash`, `source_url`, `extracted_by`,
`model_version`. Every read filters `WHERE as_of <= t`.

Never write code that:
- backfills `first_detected_on` for a macro force
- revises a historical intensity value
- retrieves data without an as-of filter
- lets outcome data reach a prompt that makes a forward-looking judgment

Look-ahead bias is invisible in code review. Assume any convenience that
touches history is a bug.

### R3 — The model proposes, the data disposes
LLM outputs are hypotheses with attached tests, never stored conclusions.
A counter-thesis produces falsifiable conditions with observation dates;
deterministic code checks them later. The model does not get to reinterpret
its own conditions after the fact.

## Stack

- Python 3.12, FastAPI, SQLAlchemy
- Postgres 16 + pgvector (single instance; do not add services until
  measurements justify it). No TimescaleDB: it is unavailable on AWS RDS and
  Cloud SQL, and plain Postgres handles the price volumes in scope. Revisit
  only with a measured need.
- Upstox API v3 for OHLCV (daily/weekly/monthly from Jan 2000; instrument
  keyed by ISIN, e.g. `NSE_EQ|INE848E01016`)
- BSE/NSE XBRL for financial facts — the source of truth
- Pydantic for every LLM output schema; no free-text model responses
- pytest, with property tests on all financial computations

## Deployment

Docker Desktop now; cloud Docker or Kubernetes with a managed Postgres later.
Keep the code portable to that from day one:
- One image (`Dockerfile`) for every environment; no host paths, no local state
- Config only via `EQUITY_*` env vars (Secrets/ConfigMaps later)
- Migrations run as a separate one-shot step (`migrate` service → K8s Job),
  never on app startup
- Before relying on a Postgres extension, confirm the target managed service
  supports it

## Data model

Entity key is **ISIN**, not ticker. Tickers change; store the mapping
history. ISIN itself changes on demergers and amalgamations — handle via a
corporate-action-driven entity table, never by string matching.

### Knowledge stores
1. `financial_facts` — XBRL line items + computed ratios + durability metrics
2. `guidance_claim` — management commitments with `hedge_strength`
   (will > expect > aim to > working towards), auto-resolved against ①.
   Status includes `SILENT` for claims that stop being mentioned.
3. `force` / `force_intensity` / `force_exposure` — macro headwinds and
   tailwinds as intervals; exposure matrix is **hardcoded and signed**
4. `relationship_edge` — RPT, subsidiary, peer. Confidence:
   `named | described | inferred`. Inferred edges never route contagion.
5. `company_event` — red flags. Severity computed by code, never asserted
   by a model. Every row needs `evidence_url`.
6. `sub_index` — hand-curated micro-indices; constituents never
   LLM-assigned
7. `order_win` / `order_status` — declared vs executed order tracking
8. `rating_action` — CRISIL/ICRA/CARE timeline. `withdrawn` is a severity-2
   signal.
9. `portfolio_risk_snapshot` — weekly, per user

## Code conventions

- Financial computations live in `core/compute/`, pure functions, fully
  tested, no I/O
- Parsers in `ingest/`, one module per source, each declaring what store it
  writes to
- LLM calls only in `extract/` and `narrate/` — nowhere else
- All LLM calls go through the gateway wrapper; never call a provider SDK
  directly
- Money as `Decimal`, never float
- All timestamps timezone-aware, IST for market data
- Migrations via Alembic; every schema change is a migration
- Every venv has a pinned `requirements.txt` next to it so it can be
  rebuilt from scratch. Regenerate it in the same change as any dependency
  add/remove/upgrade: `pip freeze --exclude-editable` (drop pip/setuptools).
  `pyproject.toml` declares ranges; `requirements.txt` is the exact rebuild.
  Never commit a dependency change without both.

## Testing

- Any function touching money or dates needs tests before it's considered done
- Extraction accuracy is validated against Screener on a sample; a failing
  validation reverts the model tier rather than lowering the threshold
- Backtests run only through the containerised replay harness, pinned by
  config + data snapshot hash. No ad-hoc notebook backtests.

## Current phase

MVP: guidance credibility ledger for 20 companies.
1. Postgres + XBRL parser → `financial_facts` ✅ when it matches Screener
2. CWIP → gross block step-function detector (pure arithmetic, no LLM)
3. Guidance extraction from concall transcripts → `guidance_claim`
4. Auto-resolution + `SILENT` detection → delivery rate per company

Do not build: agent orchestration, frontend, multi-user, execution.
Those come later and are worthless on empty stores.

## Things to push back on

If I ask for any of these, say no and explain:
- "Just have the LLM calculate it" — violates R1
- "Backfill the force timeline so we have history" — violates R2
- "Let the model pick the peer set / sector mapping" — violates R1
- Adding a service (Kafka, separate vector DB, Redis) before measurements
  show the single Postgres is inadequate
- Building the swarm before the stores have data
