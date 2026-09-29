# Getting Started — Build Plan

Indian equity knowledge system. Family use, long-horizon buy-and-hold.

---

## Day 1 — Setup

**Install Claude Code**

```bash
# macOS / Linux / WSL
curl -fsSL https://claude.ai/install.sh | bash

# Windows PowerShell
irm https://claude.ai/install.ps1 | iex

# or Homebrew
brew install --cask claude-code
```

Requires a Pro, Max, Team or Enterprise subscription, or a Console account. If you'd rather avoid the terminal, the desktop app bundles Claude Code with side-by-side sessions and visual diffs.

**Create the project**

```bash
mkdir equity-system && cd equity-system
git init
# copy CLAUDE.md to the repo root
claude
```

**Verify it read the rules.** First prompt:

> Read CLAUDE.md and summarise the three non-negotiable rules in your own words. Then tell me what you'd refuse to do.

If it can't state R1–R3 back and name a refusal, the file isn't landing. Fix that before writing code.

---

## Week 1 — Substrate

Session 1 — schema
> Set up the project skeleton. Postgres schema for `financial_facts` with the as_of convention from CLAUDE.md, Alembic migrations, pytest harness, and a `core/compute/` package for pure functions. No parsers yet.

Session 2 — architecture tests
> Write `tests/test_architecture.py` using Python's ast module. Assert: only `extract/` and `narrate/` import the LLM gateway; no float annotations on monetary fields; every SQLAlchemy model has as_of, content_hash, source_url; `core/compute/` imports nothing with I/O. Wire it into CI.

Session 3 — price ingestion
> Build the Upstox v3 historical ingester. Instrument keyed by ISIN, not ticker. Daily candles from Jan 2000. Store raw prices plus corporate-action adjustment factors separately. Also load `index_membership` for Nifty 50 and Nifty Next 50 as dated intervals. Upstox has no index-constituents API, so use NSE Indices sources: current constituent CSVs plus historical inclusion/exclusion announcements and archived reports. Map every constituent to an ISIN; never match on company name. Constituents that cannot be matched go to quarantine for manual review. Build every source as an adapter: save the raw response to blob storage first, validate it strictly with Pydantic, keep endpoints in config, and add contract tests on recorded responses plus a daily canary. Scrape politely and never work around CAPTCHAs or bot blocking; fall back to a drop folder of files downloaded by hand. Extend `tests/test_architecture.py` so only `ingest/` and `gateway/` may import the `mcp` client. Build the shared adapter base class: each adapter declares its source class (`official_api`, `official_archive`, `web_scrape`, `manual_drop`) and target store, and honours `EQUITY_SOURCE_<NAME>_ENABLED`, `EQUITY_WEB_SCRAPING_ENABLED` and `EQUITY_DEPLOYMENT_MODE` (see CLAUDE.md "Data sources"). Add an NSE bhavcopy adapter as the cross-check for Upstox prices and the source of the dated ticker-to-ISIN map. Load the core of `corporate_action` (splits, bonuses, rights, demergers, dividends, ISIN changes) to derive adjustment factors known at `t` and to update the entity table. A ratio extracted from a PDF needs human verification before it adjusts any price.

Session 3 is built in slices, one session each:
- Finished slices (3a–3e, 4a–4c) are recorded in [docs/finished.md](docs/finished.md).

**Then run the adjustment test yourself.** Pick a company with a known split, pull the series across that date, look for a discontinuity. No gap means adjusted; a cliff means raw. Do it for a bonus issue too.

**Week 1 exit criteria:** architecture tests pass, price history loaded for the Nifty 50 + Nifty Next 50 universe (100 companies), entity table keyed by ISIN, index membership stored as dated history.

---

## Week 2 — Financial facts

Session 4 — XBRL parser
> Build the BSE/NSE XBRL parser for `financial_facts`. Deterministic element mapping, no LLM. Unmapped elements go to a quarantine table for manual review, never guessed.

Session 4 is built in slices:
- **Live results-listing adapter** ✅ built, not yet run live (see `docs/finished.md`).
- **Not yet:** pre-2020 taxonomies (quarantined as `unsupported_taxonomy`); segment facts. Results XBRL has no gross block, only net PPE and CWIP; the gross-block source is built (Screener schedules adapter, see `docs/finished.md`).

Session 4b — shareholding pattern
> Parse quarterly shareholding-pattern XBRL into `shareholding_pattern`: share counts (not just percentages) for promoter, FII/FPI, DII by type and public, plus pledged shares. `as_of` must be after quarter end. Same deterministic mapping and quarantine as Session 4.

- **Not yet:** the live NSE shareholding listing adapter is built (7d, see `docs/finished.md`); named-holder facts (typed dimensions: each promoter and each holder above 1%) are counted and deferred.

Session 5 — ratios
> Implement ratio computation in `core/compute/ratios.py`. Pure functions, Decimal throughout, property tests. ROCE, margins, debt ratios, incremental ROCE.

- **5 — ratios** ✅ recorded in [docs/finished.md](docs/finished.md).
- **Check in Session 6:** Screener counts lease liabilities as borrowings, which results XBRL does not tag separately; whether Screener uses average or closing capital employed; whether its equity and net profit include non-controlling interests. Changing any of these is a new rule version. **Not yet:** ratios for banks, NBFCs and insurers (ROE, NIM, cost to income).

Session 6 — validation
> Build a validation script comparing our computed ratios against Screener for the 20-company validation sample (10 Nifty 50 + 10 Next 50, fixed seed, required company types swapped in; see CLAUDE.md "Current phase"). Report divergences with the underlying line items.

- 6a (Screener template) is recorded in [docs/finished.md](docs/finished.md).
- **6 — validation tooling** ✅, **gate not yet run** (needs data):
  - **Sample:** `validate/samples.py` `validation-sample/1`, frozen by a test. Seeded SHA-256 draw (`core/compute/sample.py`) over the NSE archive lists published by 2026-09-19. One swap, KOTAKBANK → SBILIFE, with its reason: no insurer was drawn, and the universe has no general insurer. `python -m validate sample` redraws from the recorded lists in the database. It draws from one list, not from computed membership, which is uncertain after the last list.
  - **Screener adapter:** `screener_export_drop` (`manual_drop`) stores hand-exported Screener workbooks. `screener_export` / `screener_value` are append-only, in absolute rupees. The sidecar names the ISIN and consolidation. The ISIN is checked against the constituent lists at export time, and every period must end before the export.
  - **Automated export (6b):** `screener_export_live` fetches the workbooks itself (login, company page, CSRF-protected export POST) and feeds the same loader as the hand drop. Built and tested against a mock transport only; **not verified live**, so the export path and form fields (constants in `ingest/screener_export_live/adapters.py`) may need one correction on the first run. Needs `SCREENER_USERNAME` and `SCREENER_PASSWORD` in the local `.env`, and the user must confirm that Screener's terms permit automated, logged-in export before switching it on (`EQUITY_WEB_SCRAPING_ENABLED` and `EQUITY_SOURCE_SCREENER_EXPORT_LIVE_ENABLED`). No migration.
  - **Comparison:** `core/compute/screener_compare.py` (`screener_compare/1`) pairs Screener lines with sums of our line items for each filing family. It computes ratios from Screener's lines with the ratios/1 functions.
  - **Report:** `python -m validate screener [--as-of] [--years] [--isin] [--all]` prints each divergence with its line items, the lease, NCI and ROCE evidence, and the gate. It exits 1 on FAIL.
  - **Tolerances:** lines within 1% or 0.05 crore; margins and ROCE within 0.005.
  - **Gated:** Ind AS, NBFC and bank lines, and the Ind AS ratios. Insurer lines, other income, net debt and closing ROCE are reported but not gated.
- **Session 6 findings so far:**
  - **Leases:** results XBRL does not tag lease liabilities. The report lists each case where Screener's borrowings exceed ours, beside that year's lease payments.
  - **NCI:** the report says whether Screener's profit and equity agree with our total or with the owners' share.
  - **ROCE:** average versus closing stays open. The Data Sheet has no ratios, so answering needs Screener's displayed ROCE.
  - **Borrowings:** ratios/1 needs both current and non-current borrowings facts. A filing that omits a nil line leaves debt ratios as `missing_input`; if the real data shows this, that is a ratios/2 decision.

**Week 2 exit criteria — the real gate.** Your numbers match Screener for the 20-company validation sample. If they don't, stop and fix. Everything downstream inherits these errors.

---

## Week 3 — First real signal

Session 7c — corporate actions and ownership wiring (remainder)
> The five ownership and catalyst tables, their adapters, the live NSE announcements adapter, the ownership rules, and the corporate_action lifecycle with dilution facts and the demerger chain are done (see `docs/finished.md`). What remains: use-of-proceeds extraction, written to `guidance_claim`; and feeding the ownership rules (`ownership_rules.py`, `ownership_trends.py`) real holdings once a broker adapter exists (the X and Y thresholds stay caller-supplied). Still uncovered from CLAUDE.md: the minimum-public-shareholding sale filter and the monthly `mf_holding` early read, both blocked on data that is not stored yet.

Session 7d — live shareholding listing ✅ recorded in [docs/finished.md](docs/finished.md)

Session 7e — order wins (extractor, tables, adapter and reader built; see [docs/finished.md](docs/finished.md))
> **Not yet:** the PDF quote path (value, customer and period from the PDF as a model-returned verbatim quote checked by code; today only the announcement text is read); the suppress-if-already-ran gate (needs caller-supplied price thresholds; it must report the gate that blocked); `order_status` resolution against later filings; narrow counterparty and period patterns. Screener full-text search is not the source: robots.txt disallows `/*?q=`.

---

## Week 4 — The MVP

Session 9 — guidance extraction ✅ recorded in [docs/finished.md](docs/finished.md)
- **Not yet:** a live adapter that follows exchange announcements to transcript PDFs (drop folder only); scanned transcripts (no OCR, quarantined as `no_text_layer`); the extraction run itself, which needs the 20-company sample and a real API key.

Session 10 — resolution ✅ recorded in [docs/finished.md](docs/finished.md)
- **Not yet:** the 5% point tolerance and the two-call SILENT window are unvalidated placeholders (choose on one period, measure on a later one); quarterly balance-sheet and cash-flow facts may not be stored, so claims on them stay OPEN; metrics with no XBRL source (gross margin, order book, volumes, store counts and the rest) stay unresolvable until a source exists; arithmetic is checked against hand-computed examples, not yet against Screener or real filings; bank, NBFC and insurer filers are marked unresolvable rather than graded on their own line items; whether SILENT should count against management in the delivery rate is undecided.

---

## Working rhythm

**One task per session, then `/clear`.** Resuming a two-hour session to fix a typo re-bills the entire history. Fresh sessions on a well-structured repo are cheap.

**Point at files.** "Fix the ratio calc in `core/compute/ratios.py`" costs one read. "Fix the ratio calculation" costs a search and several speculative reads.

**Plan before generating.** For anything non-trivial, ask for the approach, agree, then implement. Two paragraphs of planning beats 400 lines in the wrong shape.

**Let tests carry feedback.** `pytest -x --tb=short` is a compact failure signal. Pasting long logs is the expensive alternative.

**Commit after every green test run.** Claude Code handles git conversationally — "commit this with a descriptive message."

---

## Do not build yet

Agent orchestration · frontend · multi-user · broker execution · portfolio agents · sub-indices · force timeline.

All of it is worthless on empty stores, and LangGraph will be rewritten twice before your knowledge stores are. Agents on three years of guidance data produce something nobody can sell you. Agents on an empty database produce confident nonsense.

---

## After the MVP, in order

1. Control flow graph pass — outcome enumeration and tool allowlists per node, **before** more parsers, since it changes your schemas
2. Force timeline ③ and the hardcoded exposure matrix, including daily FII/DII market flows (`market_flow`, provisional and final rows)
3. Order book ledger ⑦, and the rest of the special situations (Session 7e; 7g and 7h are built, see `docs/finished.md`): every one raises a review or a watchlist entry, never an order, and none triggers anything until replay on a separate later period says it predicts something
4. Reverse DCF and tri-scenario valuation, producing `valuation_baseline`: target price, drawdown stop and review-named actions (`ADD_REVIEW`, `TRIM_REVIEW`, `EXIT_REVIEW`), with the assumed-baseline disclaimer enforced by a test (see CLAUDE.md "Decision support")
5. Thesis tracker (`thesis_condition` checked by code, R3) and catalyst calendar (`scheduled_event` from exchange announcements)
6. `ADD_REVIEW` positive triggers (two-stage expansion, fundamental upgrade, rating upgrade, tailwind) and their four gates, validated by replay on a separate period. The replay itself is already built, ahead of the stores that feed it: `core/compute/replay.py` raises the gated reviews from signals and the state known at `t` (a signal or state dated later is `LookAhead`), measures forward and excess returns as outcome data held in a separate type, and refuses to report a hit rate on the window its parameters were tuned on. `core/compute/replay_report.py` renders the counterfactual — what the rules would have raised, which gate blocked what, and what the price then did — carrying the assumed-baseline disclaimer, which a test asserts. Wire the stores into it as each one lands; until they do it has nothing to replay. Note the deliberate absence: the rendered report never contains a three-letter verdict, and `tests/test_architecture.py` enforces that on `core/compute/`, `core/db/models.py` and `narrate/` (a broker's own rating, recorded verbatim in `extract/`, is source data and is exempt)
7. Session 7d: monthly mutual fund holdings (`mf_holding`), MSCI and other index review announcements, and `index_event` impact analysis (days of volume)
8. Relationship graph ④, events ⑤
9. Session 7f: news, brokerage calls and scuttlebutt (see CLAUDE.md "News, brokerage calls and scuttlebutt"). Publisher RSS adapter (`web_scrape`) into `news_item`; mention extraction by the model, ISIN resolution by code through a dated alias table, with quarantine; `brokerage_call` with target prices parsed by code from quoted text; management interviews routed to `guidance_claim`; `industry_metric` from DGCA, FADA, TRAI and NPCI monthly data; a CLI timeline report. No X.
10. Read-only MCP server over the point-in-time functions; `portfolio_risk_snapshot`, which switches on critical ownership alerts and action-required corporate-action alerts for holdings. The read-only broker holdings drop (`manual_drop`, a saved Kite `get_holdings` JSON, `holdings_as_of`, `holding_weight_facts`) is built, see `docs/finished.md`; a live broker pull by adapter code and `portfolio_risk_snapshot` are not
11. Then, and only then, the swarm. No agent gets an order-capable tool.

---

## The one thing to get right this week

`as_of` on every table, filtered on every read, never backfilled.

Retrofitting it onto populated tables costs weeks. Getting it right on an empty schema costs an afternoon. It's also what makes replay possible — being able to ask "what did the system know in March 2027" without hindsight is the property that makes everything else trustworthy.
