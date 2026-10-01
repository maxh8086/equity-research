# Architecture decisions

Read this file at the start of every session and after context compaction.
Rules live in `CLAUDE.md`; this file records why the structure is the way it is.

## ADR-001: Code navigation uses the codebase-memory graph first

- **Status:** accepted.
- **Context:** the repo has about 290 files and 7,500 graph nodes. Reading or
  grepping files to find code costs tokens and misses relationships.
- **Decision:** use `codebase-memory-mcp` before any file search.
  1. `search_graph` to find symbols and domains.
  2. `trace_path` for callers, callees and dependencies.
  3. `get_code_snippet` for source of one symbol.
  4. `query_graph` and `get_architecture` for multi-hop and overview questions.
  5. `check_index_coverage` for every file relied on. Fall back to grep or file
     reads only for literals, configs, non-code files, files the index
     excludes (for example `.env`, `.claude/`), or code on branches not yet
     indexed. Say so when falling back.
- **Consequence:** re-index (`index_repository`) after merging branches, or the
  graph misses them. Indexed project name:
  `C-Users-vaibh-Downloads-Projects-Cluade-Agent-Swarm-Market-Analyst`.

## ADR-002: Layering, enforced by `tests/test_architecture.py`

- `core/compute/` is pure (no I/O, no DB, no clock): code computes, LLMs narrate (R1).
- Store tables are read only through `core/db/pit.py`, always `as_of <= t` (R2).
- `ingest/` has one adapter per source. Each declares its source class and
  target store, stores raw bytes by `content_hash` first, and validates with a
  strict Pydantic model.
- LLM calls live only in `extract/` and `narrate/`, through `gateway/`, the only
  code that imports provider SDKs.
- Hardcoded by design (under a `rule_version`): exposure matrix, sub-index
  constituents, rating scales, trigger thresholds. API details are config.

## ADR-003: Single Postgres plus write-once blob storage

- Postgres 16 with pgvector. No new services until measurement justifies one.
  Blob storage (MinIO locally, cloud blob later) is the one approved exception,
  behind one small interface, keyed by `content_hash`.
- Migrations are a separate one-shot step, never run on app startup.

## ADR-004: Holdings and Screener data come through replaceable providers

- **Status:** implemented on branch `providers-module` (unmerged, based on `cfg-wave2`).
- **Decision:** the file drop folder for holdings is removed. Data arrives as
  typed in-memory objects from a separate `providers/` package with Protocol
  interfaces (`HoldingsProvider`, `ScreenerProvider`) and concrete modules
  (Kite, Screener). `providers/registry.py` selects one from `EQUITY_*` config,
  so switching provider means config plus one module.
- Kite access is read-only (holdings and profile, never orders). Credentials
  come from the environment only. Scraping providers obey
  `EQUITY_WEB_SCRAPING_ENABLED` and are refused in `commercial` mode.
- `broker_holding` tables, migration 0021 and the point-in-time readers stay.

## ADR-005: Control-flow graph of node outcomes

- **Status:** implemented on branch `cfg-wave2` (unmerged); remaining items
  are tracked in `docs/plans/control-flow-graph-pass.md`.
- Every pipeline node declares its possible outcomes (`core/flow/`), and the
  architecture tests fail on an undeclared outcome or a stale capability gap.
