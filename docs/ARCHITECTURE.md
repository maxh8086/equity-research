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

## ADR-004: Related-party order concentration flag

- **Status:** Implemented (merged to main, commit 3bb7cfb).
- **Decision:** detect concentrated order flows to related parties (RPT) as a
  potential risk signal. The detector computes cumulative promoter order value
  over a rolling window against their stated revenue.
- **Implementation:** migration 0025, `core/compute/ownership_trends_v2.py` for
  the computation, `core/resolve/ownership_flag.py` for flagging, full test
  coverage. Flags promoter sales patterns and pledged share changes.

## ADR-005: Circular order-flow detector

- **Status:** Implemented (merged to main, commit 11faa01).
- **Decision:** detect circular or self-dealing order patterns where companies
  place orders with related entities in a cycle, creating the appearance of
  market activity.
- **Implementation:** migrations 0026 and 0027, `core/compute/order_flow_cycles.py`
  for cycle detection, `core/resolve/order_flow_cycle_flag.py` for flagging
  violations. Full test coverage includes graph traversal and cycle-breaking
  heuristics. Order book data feeds the detector.
