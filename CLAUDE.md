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
- Object storage for raw source files (XBRL, concall transcripts, rating
  PDFs): cloud blob storage when deployed, MinIO on the laptop. This is the one
  approved exception to "no new services": containers have no durable disk,
  and a parser fix must re-parse stored bytes without re-downloading. Objects
  are keyed by `content_hash` and write-once (bucket immutability on). Postgres
  records each file's `source_url`, `as_of` and fetch time. Access goes through
  one small interface, because Azure Blob is not S3-compatible.
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

Time semantics (event time vs `as_of` vs `recorded_at`, lifecycles,
intervals, replay isolation) are defined in
[docs/temporal-model.md](docs/temporal-model.md). Read it before building
or changing any store.

Entity key is **ISIN**, not ticker. Tickers change; store the mapping
history. ISIN itself changes on demergers and amalgamations — handle via a
corporate-action-driven entity table, never by string matching.

### Universe
MVP universe: **Nifty 50 + Nifty Next 50**, 100 companies (the two indices do
not overlap). Membership is stored as dated intervals in `index_membership`
(ISIN, index, `valid_from`, `valid_to`, `as_of`, `source_url`), loaded from
NSE's published constituent lists and rebalancing announcements. Both indices
rebalance in March and September. Never use today's constituent list for a
past date: that is survivorship bias, which is look-ahead (R2).

Considered and rejected for the MVP: Nifty 200; Nifty 500 and Nifty500
Multicap 50:25:25; Nifty 50 + Midcap 50 + Smallcap 50; factor and thematic
indices. Nifty500 Multicap 50:25:25 may still serve as a portfolio benchmark.

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
  directly. The gateway is its own top-level package, `gateway/`, outside
  `core/`. It is the only code that imports provider SDKs, and only
  `extract/` and `narrate/` import it.
- Store tables are read only through point-in-time functions in
  `core/db/pit.py`. `tests/test_architecture.py` enforces this and the rules
  above.
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

MVP: guidance credibility ledger for the Nifty 50 + Next 50 universe (see
Universe).

Screener validation and the first extraction run use a 20-company sample: 10
from Nifty 50 and 10 from Next 50. The sample is drawn with a fixed random seed
from membership as of a fixed date. Companies may be swapped in to make sure the
sample covers banks, NBFCs, insurers, a capital-heavy manufacturer, a group with
many subsidiaries, and any restatement or demerger case; each swap is
recorded with its reason. A sample definition is never edited: a change is a
new version. Extraction expands to all 100 only after the sample passes.

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
  show the single Postgres is inadequate. Object storage for raw source files
  is already approved (see Stack).
- Building the swarm before the stores have data
