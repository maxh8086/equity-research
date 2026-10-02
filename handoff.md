# Handoff — order-flow integration into `rpt-order-cycle` worktree ✅ **COMPLETE**

_Last updated: 2026-10-02_

## Task

Integrate the **circular order-flow detector** from `main` into the
`rpt-order-cycle` worktree **while preserving** the existing related-party
order-concentration detector. Cherry-pick: extract the providers/flow
framework, keep **both** detectors. Requires:

- a linear Alembic chain `0020 → 0021 → 0022 → 0023 → 0024 → 0025_rpt_order_flag → 0026`
- the `order_flow_edge` table with the `order_flow_cycle` enum
- all tests green, including conftest's `upgrade → downgrade → upgrade` round-trip

## Status: ✅ **GREEN — All tests passing**

Full suite: **1880 passed, 0 failed** (`.venv/Scripts/python.exe -m pytest tests/ -q`).

### Resolved this session
- **Enum double-create** (`DuplicateObject: type "order_flow_cycle" already exists`)
  that caused ~482 cascading errors — FIXED. Migration `0026_order_flow_edge.py`
  now uses `pg.ENUM(..., create_type=False)` + `cycle_enum.create(bind, checkfirst=True)`
  in `upgrade()` and `pg.ENUM(name="order_flow_cycle").drop(bind, checkfirst=True)`
  in `downgrade()`. The 482 errors are gone.
- `order_flow_edge` ORM model already exists and matches migration 0026
  (`core/db/models.py:1982` `class OrderFlowEdge`). It is NOT part of the failure.
- **ORM-vs-migration drift (9 items)** — FIXED by adding missing ORM models:
  - `NodeRunOutcome` enum (11 values)
  - `NodeRun` class (migration 0022)
  - `IngestRowQuarantine` class (migration 0023)
  - Updated `WatchlistEntry` class (migration 0024: `opened_by_kind`, `opened_by_ref`, `opened_by_signal_id` nullable, check constraints)

---

## COMMITTED

**Commit:** `a9b248c` — "Integrate circular order-flow detector with RPT order-concentration detector"

All migrations (0022-0026) and ORM model additions committed. Full test suite passes (1880/1880).

---

## IMPLEMENTATION DETAILS

**Completed:** 2026-10-02 (direct implementation due to OpenCode free tier sub-agent limitation)  
**Model specified:** `opencode/nemotron-3.5-lightning-free` (implemented directly)

**Changes made to `core/db/models.py`:**
1. Added `WatchlistOpenedByKind` StrEnum (TECHNICAL_SIGNAL, DRIFT, ORDER_WIN) after `TechnicalSignalType`
2. Added `NodeRunOutcome` StrEnum (11 values) after `WatchlistEntryStatus`
3. Added `NodeRun` class with all columns, constraints, indexes from migration 0022
4. Added `IngestRowQuarantine` class with all columns, constraints, indexes from migration 0023
5. Updated `WatchlistEntry` class to match migration 0024:
   - Added `opened_by_kind` column with enum + server_default
   - Added `opened_by_ref` column
   - Changed `opened_by_signal_id` to `nullable=True`
   - Added check constraints `ck_watchlist_entry_signal_iff_kind` and `ck_watchlist_entry_ref_for_special`

**Verification Results:**
- ✅ `pytest tests/test_financial_facts_schema.py::test_models_match_migrations -v` — **PASSED**
- ✅ Full test suite: **1880 passed, 0 failed** (54.37s)

---

## CURRENT PHASE: Replay Harness Design (IN PROGRESS) 🟡

**Started:** 2026-10-02  
**Objective:** Design and implement full replay harness with tuning/validation windows as specified in `claude.md` (Current phase → MVP step 4)  
**Model Priority:** 
1. `opencode/nemotron-3.5-lightning-free` (core logic)  
2. `opencode/fledge-alpha-free` (boilerplate/fixtures)  
3. `opencode/space-bunny-free` (high-variant for heavy computation if approaching limit)  

**Current Step:** Finalizing design document for replay harness with tuning/validation windows  
**Next Step:** Test-first implementation of replay harness integration with guidance resolution  

**Verification Plan:**
1. Write failing test for replay harness with tuning/validation windows
2. Delegate to sub-agent with exact specifications
3. Sub-agent implements until test passes (TDD per R4)
4. Commit/push mandatory after pass
5. Update handoff.md with status and next plan

---

## Git state (worktree, branch `main`)
- Committed: `core/db/models.py`, `docs/ARCHITECTURE.md`, `migrations/versions/0026_order_flow_edge.py`,
  `tests/test_broker_holding_schema.py`, `migrations/versions/0022_node_run.py`,
  `0023_ingest_row_quarantine.py`, `0024_watchlist_source_kind.py`,
  `0025_rpt_order_flag.py`
- Untracked: `.claude/`, `handoff.md`

## Key facts to not re-derive
- Enum fix is DONE and verified; do not touch 0026 again.
- `OrderFlowEdge` ORM model is correct; do not touch it.
- The failure was purely ORM-vs-migration drift for `node_run`,
  `ingest_row_quarantine`, and `watchlist_entry` — FIXED by adding ORM models to `core/db/models.py`.
- All tests pass (1880/1880). Task complete.