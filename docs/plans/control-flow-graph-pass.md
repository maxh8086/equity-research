# Control flow graph pass: design plan

Status: proposal, docs only. Written against `main` at 1f2afa6 (migration head `0020`).
No code, tests or migrations are part of this change. Paths marked "(new)" do not exist
yet; every other path cited was checked to exist.

Post-MVP step 1 in `getting-started-plan.md` ("After the MVP, in order"):
"Control flow graph pass - outcome enumeration and tool allowlists per node,
before more parsers, since it changes your schemas."

---

## 1. What the pass is, and why it comes before more parsers

### What it is

The pipeline has stages (adapters in `ingest/`, one extractor in `extract/`, pure
detectors in `core/compute/`, readers in `core/db/pit.py`, report entry points). Each
is written and tested on its own, but nothing states, in one place a test can read:

1. which stages exist and what each reads and writes;
2. every way each stage can end (its outcomes), and where each ending is recorded;
3. what each stage may touch (network hosts, the model gateway, blob storage,
   database writes, the clock, and for any future agent step, which tools).

The pass produces that statement as data (a registry of nodes) plus tests that make
the code agree with it. It is a verification graph, not an orchestrator.
`getting-started-plan.md` "Do not build yet" lists agent orchestration; the pass builds
no scheduler, runner, agent loop or dependency. Nodes are declarations; edges are the
store tables one node writes and another reads.

### Why it precedes more parsers

Every remaining parser (news, brokerage calls, industry data, MF holdings, index
reviews, force timeline) must answer the same questions the existing ones answered ad
hoc: what happens on quarantine, on a blocked source, on no data, on a future-dated
input. Today the answers differ per adapter and several are not durable (section 3).
Building more parsers first multiplies the inconsistency and then requires altering
populated append-only tables. Two schema gaps found (section 5):

- there is no durable record of a run outcome (only stdout and an exit code in
  `ingest/__main__.py`), so a blocked or empty source is invisible after exit;
- `watchlist_entry.opened_by_signal_id` is NOT NULL and references `technical_signal`
  (`core/db/models.py`, class `WatchlistEntry`), so drift and order-win special
  situations cannot raise a stored watchlist entry. `core/db/pit.py` has only a
  read-side drift watchlist projection.

### Constraints and how the design respects them

| Rule | How |
|---|---|
| R1 deterministic | Registry, outcomes and allowlists are code. No model picks an outcome, reason or tool. |
| R2 as_of, never backfilled | New tables use `ProvenanceMixin` (`core/db/base.py`) and are append-only. Replays never write `node_run` (`docs/temporal-model.md`). |
| R3 model proposes | The one model node (`extract/`) keeps its verbatim-quote checks; its outcomes are recorded, quarantine reasons unchanged. |
| R4 tests first | Every slice in section 6 names its first failing test. |
| No order-capable tool | Section 4: hardcoded denylist plus default-deny allowlists, tested. |
| Special situation raises review or watchlist, never an order | `OutcomeKind` has `RAISED_WATCH` and `RAISED_REVIEW` and nothing that names a trade; a test scans the enum. |
| as_of on every table | New tables and the altered one keep provenance columns; the existing `test_orm_metadata_has_provenance_columns` covers new models. |
| No verdict words in `core/compute`, `core/db/models.py`, `narrate/` | Outcome and reason names use `*_REVIEW`, `WATCH`, `QUARANTINED`; the existing verdict-word rule checks them. |
| Allowlists enforceable by an architecture test | Section 4. |

---

## 2. Inventory: candidate graph nodes

Network and tool access is read from imports. `httpx` is used by `ingest/http.py`,
`ingest/nse_session.py` and the networked adapter modules below; `core/blob.py` talks
to the S3-compatible blob store. `gateway/` is the only package importing a provider
SDK. No module imports an MCP client today (the architecture test permits `ingest/`
and `gateway/` only). There is no `narrate/` package and no agent step yet.

### 2.1 Ingest adapters (23 concrete `Adapter` subclasses)

Writes are the declared `target_stores`; every adapter storing raw bytes also writes
`raw_source_file` via `Adapter.store_raw` in `ingest/base.py`.

| Node (adapter `name`) | Module | Class | Reads | Writes | Net |
|---|---|---|---|---|---|
| `nse_indices_constituents` | `ingest/nse_indices/adapters.py` | official_archive | archive host (config) | index_snapshot, index_snapshot_constituent, index_snapshot_quarantine, entity, entity_isin | yes |
| `nse_indices_constituents_drop` | same | manual_drop | drop folder | same | no |
| `wayback_nse_indices_constituents` | same | web_scrape | Wayback CDX and capture URLs (config) | same | yes |
| `nse_bhavcopy` | `ingest/nse_bhavcopy/adapters.py` | official_archive | archive URL template (config) | nse_bhavcopy_row, nse_bhavcopy_quarantine | yes |
| `nse_bhavcopy_drop` | same | manual_drop | drop folder | same | no |
| `upstox_daily_candles` | `ingest/upstox/adapters.py` | official_api | Upstox (config), token | upstox_candle, upstox_candle_quarantine | yes |
| `upstox_daily_candles_drop` | same | manual_drop | drop folder | same | no |
| `nse_corporate_actions_drop` | `ingest/corporate_actions/adapters.py` | manual_drop | drop folder | corporate_action, corporate_action_quarantine | no |
| `corporate_actions_curated_drop` | same | manual_drop | drop folder | same | no |
| `nse_xbrl_results_drop` | `ingest/nse_xbrl/adapters.py` | manual_drop | drop folder | financial_facts, financial_filing, financial_facts_quarantine | no |
| `nse_shareholding_drop` | `ingest/nse_shp/adapters.py` | manual_drop | drop folder | shareholding_pattern, shareholding_filing, shareholding_quarantine | no |
| `nse_shareholding_listing` | `ingest/nse_shareholding/adapters.py` | web_scrape | NSE listing API (config) | same three | yes |
| `screener_export_drop` | `ingest/screener_export/adapters.py` | manual_drop | drop folder | screener_export, screener_value | no |
| `screener_schedules` | `ingest/screener_schedules/adapters.py` | web_scrape | screener.in (config) | financial_facts (+ quarantine) | yes |
| `concall_transcript_drop` | `ingest/concall_drop/adapters.py` | manual_drop | drop folder | raw_source_file, concall_document, guidance_quarantine | no |
| `insider_trade_drop` | `ingest/ownership_events/adapters.py` | manual_drop | drop folder | insider_trade | no |
| `stake_disclosure_drop` | same | manual_drop | drop folder | stake_disclosure | no |
| `bulk_block_deal_drop` | same | manual_drop | drop folder | bulk_block_deal | no |
| `scheduled_event_drop` | same | manual_drop | drop folder | scheduled_event | no |
| `index_event_drop` | same | manual_drop | drop folder | index_event | no |
| `rating_action_drop` | `ingest/rating_action/adapters.py` | manual_drop | drop folder | rating_action | no |
| `nse_announcements` | `ingest/nse_announcements/adapters.py` | web_scrape | NSE announcements API (config) | scheduled_event | yes |
| `nse_order_wins` | `ingest/nse_order_wins/adapters.py` | web_scrape (lineage) | stored raw announcement listings, blob | order_win, order_win_quarantine | no |

The exact adapter list and store names must be re-derived from
`ingest/registry.py` by slice A1; the table is a reading of the code at 1f2afa6.

Two facts the graph must model rather than infer from `source_class`:

- `nse_order_wins` declares `web_scrape` but does no network I/O. It is a derive node
  (reads stored raw files, writes a store). A node's `kind` and its capabilities are
  separate fields.
- `nse_order_wins` imports the `nse_announcements` adapter for listing lineage; that
  cross-adapter edge is real and should be declared as an edge.

### 2.2 Model node and gateway

| Node | Module | Reads | Writes | Net / tools |
|---|---|---|---|---|
| `extract_concall` | `extract/concall.py`, `extract/guidance.py` | concall documents via `core/db/pit.py`, blob text | guidance_claim, guidance_quarantine | model calls only, through the gateway |
| `gateway` (infrastructure) | `gateway/client.py`, `gateway/providers.py`, `gateway/untrusted.py` | prompt text | nothing | provider SDK network. `complete()` has no `tools` parameter; the provider passes one internal schema-forcing pseudo-tool named `record`, a structured-output device, not an agent tool |

### 2.3 Pure compute nodes (`core/compute/`, `core/resolve/`)

`tests/test_architecture.py` already gives every one of these an empty capability set
(stdlib plus `core.compute` imports only, no I/O calls, no clock).

| Node | Module |
|---|---|
| adjustment | `core/compute/adjustment.py` |
| ratios | `core/compute/ratios.py` |
| cwip detector and resolver | `core/compute/cwip_detector.py`, `core/resolve/cwip_commissioning.py` |
| technical | `core/compute/technical.py` |
| membership | `core/compute/membership.py` |
| price crosscheck | `core/compute/price_crosscheck.py` |
| screener compare | `core/compute/screener_compare.py` |
| ratings | `core/compute/ratings.py` |
| ownership rules | `core/compute/ownership_rules.py` |
| buyback | `core/compute/buyback.py` |
| corporate action lifecycle | `core/compute/corporate_action_lifecycle.py` |
| guidance, resolution, report | `core/compute/guidance.py`, `core/compute/guidance_resolution.py`, `core/compute/guidance_report.py` |
| drift | `core/compute/drift.py` |
| order terms and wins | `core/compute/order_terms.py`, `core/compute/order_wins.py` |
| replay and its report | `core/compute/replay.py`, `core/compute/replay_report.py` |

Helpers (`core/compute/hashing.py`, `isin.py`, `disclaimer.py`, `sample.py`) are leaf
utilities, not nodes.

### 2.4 Read layer, entry points, infrastructure

| Node | Module | Access |
|---|---|---|
| pit read layer | `core/db/pit.py` | the only code allowed to query store tables; DB read only; one node, not one per function |
| guidance report CLI | `report_cli.py` | DB read, stdout |
| validation harness | `validate/__main__.py`, `validate/report.py`, `validate/sidecars.py`, `validate/samples.py` | DB read, drop-folder sidecar writes, stdout |
| Screener template builder | `tools/screener_template/build.py` | file output |
| adapter runner | `ingest/__main__.py`, `ingest/registry.py`, `ingest/base.py` | runs adapters, commits sessions; the place where outcomes are produced |
| shared HTTP | `ingest/http.py`, `ingest/nse_session.py` | raises `AccessBlocked` |
| blob | `core/blob.py` | S3-compatible client to the configured endpoint |

---

## 3. Outcomes per node: enumerated, and represented or missing

### 3.1 Proposed closed vocabulary

`OutcomeKind` (proposed in `core/compute/outcomes.py` (new), stdlib only, because
`core/compute` may import only stdlib and `core.compute`; `core/flow` imports from it):

| Kind | Meaning |
|---|---|
| `SUCCESS` | ran, produced output |
| `NO_NEW_DATA` | source healthy, nothing new (distinct from broken) |
| `NO_DATA` | inputs missing, node cannot answer; carries a reason |
| `DISABLED` | switched off; wrote nothing, substituted nothing |
| `BLOCKED` | the source refused us (401/403/429/451, robots.txt); final, never worked around |
| `QUARANTINED` | input held back with a named reason |
| `GATE_BLOCKED` | a special-situation or replay gate suppressed the signal; gate named |
| `LOOK_AHEAD_REFUSED` | an input was dated after `t` |
| `RAISED_WATCH` / `RAISED_REVIEW` | the only ways a detector acts: a watchlist entry, or `ADD_REVIEW`/`TRIM_REVIEW`/`EXIT_REVIEW` |
| `FAILED` | unexpected error |

Reasons stay in the per-node enums that already exist; the graph records which enum
backs each node's `QUARANTINED` and `NO_DATA`.

### 3.2 Adapters (all 23)

| Outcome | Present today | Missing |
|---|---|---|
| SUCCESS | `RunStatus.SUCCEEDED`, `RunResult` in `ingest/base.py` | none |
| DISABLED | `RunStatus.DISABLED` (`disabled_reason`) | none |
| NO_NEW_DATA | not a status; zero rows returns SUCCEEDED with a detail string (for example in `ingest/nse_announcements/adapters.py`, `ingest/nse_order_wins/adapters.py`) | a distinct kind, so "quiet" and "silently broken" differ |
| BLOCKED | `AccessBlocked` (`ingest/http.py`) is caught in `nse_announcements`, `nse_bhavcopy`, `nse_indices`, `nse_shareholding`, `screener_schedules` and `upstox`, becomes `tally.problems` text, and the run ends FAILED | a BLOCKED status, a durable record, a review-queue entry. Today a block leaves no row anywhere, only stdout and exit code |
| QUARANTINED (durable per row or file) | per-store tables with their own reason enums: `index_snapshot_quarantine`, `nse_bhavcopy_quarantine`, `upstox_candle_quarantine`, `corporate_action_quarantine`, `financial_facts_quarantine`, `shareholding_quarantine`, `guidance_quarantine`, `order_win_quarantine` (migration 0020) | `nse_announcements`: unresolved symbols are only counted (`rows_isin_unresolved`) then skipped, never stored. The five `ownership_events` drops, `rating_action_drop` and `screener_export_drop`: a bad row or file becomes a `problems` string and a FAILED run, with no stored evidence |
| LOOK_AHEAD_REFUSED | `Adapter.store_raw` raises when `as_of` is after fetch time; `IMPLAUSIBLE_AS_OF` quarantine reasons in facts, shareholding and guidance; `nse_order_wins` turns `order_wins.LookAhead` into `problems` (so FAILED) | a LOOK_AHEAD_REFUSED kind and record, not collapsed into FAILED |
| FAILED | `RunStatus.FAILED`; `ingest/__main__.py` rolls back and prints | a durable record |

### 3.3 Model node `extract_concall`

| Outcome | Present | Missing |
|---|---|---|
| SUCCESS | `ExtractionRun` in `extract/concall.py`; `guidance_claim` rows carry `model_version` | run-level record |
| QUARANTINED | claim-level reasons in `extract/guidance.py` mapped into the `GuidanceIssueReason` enum; whole-document `SCHEMA_REJECTED`, `NO_TEXT_LAYER` | none at row level |
| NO_DATA | a document with no forward-looking claims yields no rows and no marker | NO_DATA per document |
| LOOK_AHEAD_REFUSED | `IMPLAUSIBLE_AS_OF` | none |
| provider failure | `ProviderUnavailable` raised from `gateway/providers.py` | recorded as FAILED with a `node_run` |

### 3.4 Compute nodes

Three modules raise their own look-ahead error class: `LookAhead` in
`core/compute/drift.py`, `core/compute/order_wins.py` and `core/compute/replay.py`.
The others rely on inputs pre-filtered by `core/db/pit.py`.

| Node | Present | Missing |
|---|---|---|
| drift | `MissingInputReason` as NO_DATA; `LookAhead`; read-side watchlist projection in `core/db/pit.py` | RAISED_WATCH is not stored (schema gap, section 5) |
| guidance resolution | `Status` (MET, MISSED, OPEN, UNRESOLVABLE) and `UnresolvableReason` | look-ahead guard (pit only) |
| order wins | `QuarantineReason`; `LookAhead`; store exists (`order_win`) | RAISED_* and the suppress-if-already-ran gate (listed as unbuilt in `docs/finished.md`, Session 7e) |
| replay | `GateOutcome` (PASS, BLOCK, UNKNOWN) as GATE_BLOCKED; `LookAhead`; the three review action names | none for the enum |
| screener compare | `Status` incl. `MISSING_SCREENER`, `MISSING_OURS` | n/a (validation, not a signal) |
| buyback | `validate_tender_offer` raises a bare `ValueError` for an open-market buyback | a typed NOT_SCORED outcome |
| ownership rules, corporate action lifecycle | violations and alerts as return values | no sink: computed alerts are not raised into any store |
| technical | signals open `watchlist_entry` rows | "not enough history" reasons not audited here; slice A1's registry test forces each to be declared |
| ratios, adjustment, cwip, ratings, membership, price crosscheck | not audited in this plan | slice A1's registry test forces each to declare its outcomes |

The invariant "every special situation raises a review or watchlist entry" holds
today only for technical signals. Drift, order wins, buyback tenders, ownership alerts
and dilution compute a value but have no stored sink. That sink is the RAISED_* work.

---

## 4. Per-node tool allowlist model and its enforcement

### 4.1 Two layers

**Layer 1, capabilities (enforceable today, no agents exist).** Each node declares the
capabilities it may exercise.

| Capability | Detected by |
|---|---|
| `NETWORK_HTTP` | imports of `httpx`, `ingest.http`, `ingest.nse_session` |
| `MODEL_CALL` | import of `gateway` (already restricted to `extract/`, `narrate/`) |
| `BLOB_READ` / `BLOB_WRITE` | import of `core.blob` / `botocore`; call names such as `store_raw` |
| `DB_READ` | `core.db.pit` import (reads elsewhere are already banned) |
| `DB_WRITE` | call names `.add(`, `.add_all(`, `insert(`, `.merge(` |
| `FS_READ` | `iterdir`, `read_bytes`, `read_text`, `open(` |
| `CLOCK` | `now()` calls |

Expected sets: pure compute empty (already enforced); `extract_concall` is
{MODEL_CALL, DB_READ, DB_WRITE, BLOB_READ} and not NETWORK_HTTP; networked adapters
get NETWORK_HTTP plus declared endpoints; drop adapters never NETWORK_HTTP;
`nse_order_wins` is {DB_READ, DB_WRITE, BLOB_READ}.

Endpoints are fenced by config: `core/config.py` holds the URL settings. Each
networked node declares the `Settings` field names it uses; a test requires every
URL-typed field to be claimed by exactly one node, and every `http(s)://` literal in a
networked module to have a host present in its node's claimed defaults.

**Layer 2, agent tools (for step 11 and any earlier interactive agent).** No agent node
exists, so this layer is a registry and rules ready before the first agent.
Proposed `core/flow/tools.py` (new):

- `ToolRef = (server_alias, tool_name)`, keyed by logical alias, never a connector id.
- `ToolEffect`: `READ`, `WRITE_KNOWLEDGE` (via code-owned functions only),
  `WRITE_EXTERNAL`, `ORDER`.
- `TOOL_REGISTRY`: default deny; an unregistered tool cannot be in any allowlist.
- `ORDER_TOOL_DENYLIST`, hardcoded. From the tool listing visible to this session (not
  from the repo), the broker server exposes `place_order`, `modify_order`,
  `cancel_order`, `place_gtt_order`, `modify_gtt_order`, `delete_gtt_order` (all
  `ORDER`) and `login` (a human step per CLAUDE.md). The Screener server exposes
  `add_portfolio_stock`, `remove_portfolio_stock`, `update_portfolio_stock`, which
  mutate the user's Screener account (`WRITE_EXTERNAL`). Read tools such as
  `get_holdings`, `get_positions`, `get_orders`, `get_trades`, `get_ltp`, `get_quotes`,
  `get_ohlc`, `get_historical_data`, `get_margins`, `get_profile`, `get_mf_holdings`,
  `get_gtts`, `search_instruments` are candidates for `READ`. The slice that writes the
  registry must confirm names against the servers actually configured (decision D8).
- `NodeSpec.tools`: empty for every non-agent node. `extract_concall` is
  model-call-only: a test asserts `gateway.client.complete` has no `tools` parameter.
- `resolve_tools(node, offered)`: pure; returns `offered ∩ node.tools`, and raises if
  `offered` contains a denylisted tool even when the node did not list it (defence
  against a server adding an order tool later).

### 4.2 Enforcement in `tests/test_architecture.py`

The file's pattern: each rule is a function over parsed source files returning
`path:line: message`, registered in `RULES`, run on the repo, and run against
synthetic violations in `CASES` so a rule that stops matching fails. New rules follow
it and need no database, so they run in CI's "not db and not blob" job.

| New rule | Fails when |
|---|---|
| `node_coverage` | a `.py` under `ingest/`, `extract/`, `core/compute/`, `core/resolve/`, `gateway/`, `validate/`, `tools/` or `report_cli.py` maps to no node or to two; adapters found by `ingest.registry` differ from adapter nodes, or `target_stores` differ from a node's `writes` |
| `node_capabilities` | observed capabilities of a node's modules are not a subset of its declared set |
| `node_endpoints` | a URL-typed `Settings` field is unclaimed or double-claimed, or a networked module has a literal host outside its node's claimed defaults |
| `node_outcomes` | a declared `QUARANTINED`/`NO_DATA` reason enum does not resolve, or a declared non-planned outcome has no enum or writer |
| `outcome_vocabulary` | `OutcomeKind` has a member with order, trade, buy, sell or hold in its name; `RAISED_*` members are not exactly the watch and the three `*_REVIEW` names |
| `tool_allowlists` (in `tests/test_architecture_tools.py` (new), reusing helpers) | any node lists an unregistered, non-`READ` or denylisted tool; any registry name matching `order|gtt|trade|place|modify|cancel|transfer|withdraw|payment` is not classified ORDER, WRITE_EXTERNAL or WRITE_KNOWLEDGE |
| `no_order_tool_strings` | a string constant containing a denylisted tool name appears outside `core/flow/tools.py` and `tests/` |

Limits stated honestly: import detection is precise; call-name detection for
`DB_WRITE`, `BLOB_WRITE`, `FS_READ` and `CLOCK` is a name heuristic and can miss an
indirect call. That is why sets are declared per node and reviewed, and why the runtime
`resolve_tools` exists for layer 2. Layer 1 does not restrict the developer's own
Claude Code session, whose connector tools are outside this repo (D7).

### 4.3 Declaring nodes without file collisions

Node declarations live one file per adapter package or compute group under
`core/flow/declarations/` (new), aggregated by `core/flow/nodes.py` (new) the way
`ingest/registry.py` discovers adapters. Each Wave 2 slice owns only its own
declaration file, so flipping an outcome from planned to present never touches a
shared file.

---

## 5. Schema changes and migration order

Head on main is `0020` (`migrations/versions/0020_order_win.py`, down_revision `0019`).
Ids `0012` to `0014` are unused on main; do not fill the gap.
`tests/test_migrations.py` enforces a single head; CLAUDE.md requires
`NNNN_short_slug` ids.

| # | Change | Kind | Migration |
|---|---|---|---|
| 1 | new `node_run` (append-only): `node`, `run_id`, `outcome` (CHECK over the closed kind list), `reason`, `detail`, `rows_in`, `rows_out`, `quarantined`, `rule_version`, plus provenance. `as_of` is the run clock `t`; `source_url` is `node://<name>`; `content_hash` is sha256 of the canonical run summary via `core/compute/hashing.py`; `model_version` set only by `extract_concall`. `node` and `reason` are text validated in code, so adding a node needs no migration. Replays never write it | new table | yes, first |
| 2 | new `ingest_row_quarantine` (append-only): `adapter`, `store`, `row_ref`, `reason`, `detail`, `raw_excerpt`, `rule_version`, provenance. Covers the eight adapters with no durable quarantine today (`nse_announcements`, five `ownership_events` drops, `rating_action_drop`, `screener_export_drop`) | new table | yes, second |
| 3 | `watchlist_entry`: add `opened_by_kind` (`TECHNICAL_SIGNAL`, `DRIFT`, `ORDER_WIN`), add `opened_by_ref`, make `opened_by_signal_id` nullable, CHECK that the signal id is present exactly when kind is `TECHNICAL_SIGNAL`. Existing rows get `TECHNICAL_SIGNAL` by a constant default, not inference | alter table | yes, third |
| 4 | `order_win` and `order_win_quarantine` | already migrated | none |
| 5 | `position_review` sink for `RAISED_REVIEW` | deferred to steps 4 and 5 | later |
| 6 | pit readers: `node_runs_as_of`, `review_queue_as_of` (read-only union of the existing quarantine readers, `ingest_row_quarantine`, and BLOCKED/FAILED runs), watchlist reader extended for `opened_by_kind` | code | no |
| 7 | `RunStatus` gains BLOCKED and NO_NEW_DATA; `RunResult` gains outcome and reason | code | no |

Numbering: one slice (A2) authors all three migrations so they cannot collide:
`0021_node_run`, `0022_ingest_row_quarantine`, `0023_watchlist_source_kind`, each
chained to the previous. Before merge the author runs `git fetch`, takes the highest
number on `origin/main` plus one, rebases `down_revision`, and renames if `0021` is
taken (other in-flight sessions may claim numbers). Order reasoning: `node_run` first
because adapter and extract slices record into it; `ingest_row_quarantine` second
because adapter slices write to it; the `watchlist_entry` alteration last because it
touches a populated table and only the drift sink slice needs it.

---

## 6. Work breakdown

File ownership is exclusive: no file appears in two slices. Waves are dependency
order; slices in a wave run in parallel. Every slice writes its failing test first
(R4) and touches nothing outside its list. Where a test file is marked new, the whole
file is new; test names are proposals.

**Wave 0 (parallel, start immediately)**

| Slice | Owns | First failing test |
|---|---|---|
| A1 registry and outcomes | `core/compute/outcomes.py`, `core/flow/__init__.py`, `core/flow/graph.py`, `core/flow/nodes.py`, `core/flow/declarations/*.py`, `tests/test_flow_graph.py` (all new; declarations for every node in section 2, unbuilt outcomes tagged `planned` with the owning slice) | `tests/test_flow_graph.py::test_outcome_kinds_are_the_agreed_closed_set` |
| A2 schema | `core/db/models.py` (append `NodeRun`, `IngestRowQuarantine`; edit `WatchlistEntry`), `migrations/versions/0021_node_run.py`, `0022_ingest_row_quarantine.py`, `0023_watchlist_source_kind.py`, `tests/test_node_run_schema.py`, `tests/test_ingest_row_quarantine_schema.py`, `tests/test_watchlist_source_schema.py` (new) | `tests/test_node_run_schema.py::test_node_run_rejects_update_and_delete` |

**Wave 1 (after A1 and A2 merge)**

| Slice | Owns | First failing test |
|---|---|---|
| A3 tool registry | `core/flow/tools.py`, `tests/test_flow_tools.py` (new) | `test_no_allowlist_contains_an_order_capable_tool` |
| A4 shared look-ahead | `core/compute/drift.py`, `core/compute/order_wins.py`, `core/compute/replay.py`, `tests/test_compute_lookahead.py` (new) | `test_all_look_ahead_errors_share_one_base` (old class names stay importable; existing tests not edited) |
| A5 buyback outcome | `core/compute/buyback.py`, `tests/test_buyback.py` | `test_open_market_buyback_returns_not_scored_outcome` |
| A6 architecture rules | `tests/test_architecture.py` | `test_rule_detects[node_capabilities-...]` (synthetic module importing `httpx` under a compute node) |
| A7 tool rules | `tests/test_architecture_tools.py` (new) | `test_rule_detects_order_tool_in_allowlist` |
| A8 pit readers | `core/db/pit.py`, `tests/test_pit_node_runs.py`, `tests/test_pit_review_queue.py` (new) | `test_blocked_run_is_in_review_queue_at_or_after_its_as_of_and_not_before` |
| A9 ingest base | `ingest/base.py`, `ingest/__main__.py`, `tests/test_ingest_base.py` | `test_access_blocked_reports_blocked_not_failed` (base records a `node_run`, derives NO_NEW_DATA, keeps a non-zero exit for BLOCKED) |

**Wave 2 (after A8 and A9; adapter fan-out; each slice also flips its own declaration
file from planned to present)**

| Slice | Owns | First failing test |
|---|---|---|
| B1 announcements | `ingest/nse_announcements/*`, `tests/test_nse_announcements.py`, its declaration file | `test_unresolved_symbol_is_quarantined_not_dropped` |
| B2 ownership drops | `ingest/ownership_events/*`, `tests/test_ownership_events_adapters.py`, its declaration file | `test_rejected_file_is_recorded_in_ingest_row_quarantine` |
| B3 rating and screener export | `ingest/rating_action/*`, `ingest/screener_export/*`, `tests/test_rating_action_adapters.py`, `tests/test_screener_export_adapters.py`, declaration files | `test_bad_rating_row_is_quarantined_with_reason` |
| B4 bhavcopy and upstox | `ingest/nse_bhavcopy/*`, `ingest/upstox/*`, `tests/test_nse_bhavcopy_adapters.py`, `tests/test_upstox_adapters.py`, declaration files | `test_access_blocked_returns_blocked_status` |
| B5 indices and wayback | `ingest/nse_indices/*`, `tests/test_nse_indices_adapters.py`, `tests/test_wayback_cdx.py`, declaration file | `test_wayback_block_returns_blocked_status` |
| B6 shareholding listing and schedules | `ingest/nse_shareholding/*`, `ingest/screener_schedules/*`, `tests/test_nse_shareholding.py`, `tests/test_screener_schedules_outcomes.py` (new), declaration files | `test_screener_schedules_block_returns_blocked_status` |
| B7 order wins | `ingest/nse_order_wins/*`, `tests/test_nse_order_wins_adapters.py`, declaration file | `test_look_ahead_is_reported_as_refused_not_failed` |
| B8 extract | `extract/concall.py`, `extract/guidance.py`, `tests/test_extract_concall.py`, `tests/test_extract_guidance.py`, declaration file | `test_run_with_no_forward_looking_claims_reports_no_data` |
| B9 drift sink | `core/resolve/drift_watch.py`, `tests/test_resolve_drift_watch.py` (new) | `test_drift_watch_maps_to_watchlist_entry_with_drift_kind` |

The remaining adapters (`nse_xbrl`, `nse_shp`, `corporate_actions`, `concall_drop`, and
the drop variants of bhavcopy, indices, upstox) already have durable quarantine and need
no code change: A9's base class records their `node_run` and derives NO_NEW_DATA. Their
declaration files are written in A1.

**Wave 3**

| Slice | Owns | First failing test |
|---|---|---|
| C1 completion | `docs/finished.md`, `CLAUDE.md` (a conventions line: every new adapter or compute module needs a node declaration; and the store-11 sentence if D10 is accepted), `tests/test_flow_complete.py` (new) | `test_no_planned_outcomes_remain` |

`getting-started-plan.md` is not edited by any slice; its step 1 is recorded as done in
`docs/finished.md` by C1.

Cautions for parallel workers: B9 overlaps in subject with the local `drift-wiring`
branch; check it first. A4 changes three exception classes that existing drift,
order-win and replay tests import; it must keep old names importable and not edit those
tests.

---

## 7. Open decisions for the user

| # | Decision | Recommendation |
|---|---|---|
| D1 | Graph descriptive and test-verified only, or also executable (a runner)? | Descriptive only. A runner is the orchestration the plan says not to build yet. |
| D2 | Registry home: `core/flow/` package, or a doc only? | A package of data, one declaration file per adapter package. A doc cannot be enforced. |
| D3 | Put `OutcomeKind` in `core/compute/outcomes.py`, since compute may not import `core.flow`? | Yes; stdlib-only, imported by compute, ingest and the registry. |
| D4 | Persist run outcomes in a `node_run` table, or logs only? | Table. Without it a blocked or empty source is invisible after exit. |
| D5 | One generic `ingest_row_quarantine`, or a quarantine table per adapter as elsewhere? | Generic, reason as text checked against the node's declared enum; per-store tables cost a migration per adapter. |
| D6 | For ownership, rating and screener-export drops: keep whole-file rejection, or move to row-level quarantine? | Keep whole-file rejection and record each rejected file; row-level changes what loads and needs its own review. |
| D7 | Allowlists cover this repo's runtime agents only; connector tools in the developer's own Claude Code session (including broker order tools) are outside the repo. | Accept the boundary and separately restrict those tools in Claude Code permission settings, a change only you can make. |
| D8 | Which MCP servers and tool names go in `TOOL_REGISTRY`? Names in 4.1 come from this session's tool listing. | Register only servers you confirm are configured for the product; deny anything unlisted. |
| D9 | `BLOCKED` semantics: distinct exit code, automatic fallback to the drop adapter? | Distinct non-zero exit code and a review-queue entry; no automatic fallback (the drop folder is a human step per CLAUDE.md). |
| D10 | Make `watchlist_entry` polymorphic, or add a separate table for special-situation watches? CLAUDE.md store 11 says a technical signal opens an entry; this changes that sentence. | Polymorphic `opened_by_kind` on the existing table, and update the CLAUDE.md sentence in C1. |
| D11 | Build `position_review` now to give `RAISED_REVIEW` a sink? | No; declare it planned, it belongs to steps 4 and 5. |
| D12 | Unify the three `LookAhead` classes under one base? | Yes, keeping old names importable (A4). |
| D13 | Tool-rule tests in `tests/test_architecture_tools.py` or all in `tests/test_architecture.py`? | Separate file for parallel work; fold into `RULES` afterwards if preferred. |
| D14 | Migration ids `0021` to `0023` chained in one slice, or reserve ranges per session? | Chain in one slice and renumber at merge after `git fetch`; the single-head guard already covers collisions. |
