# Revisit list

Ideas worth adapting, collected from outside repositories. **Nothing here is
planned yet.** Plan only after every repository on the review list has been
read, so overlapping ideas are merged once and ordered together.

Each idea is checked against R1 (code computes), R2 (as_of, no look-ahead) and
R3 (model proposes, data disposes) before it is listed. Status values:
`candidate` → `planned` → `done` | `rejected`.

## Repositories reviewed

| Repo | Reviewed | Basis |
|---|---|---|
| TauricResearch/TradingAgents (v0.5.1) | 2026-09-29 | README and code read from a local clone |
| AayushH1510/portfolio-risk-engine (Varense) | 2026-09-29 | README and code read from a local clone; no licence |
| alfwro13/Stock_Analysis_Project | 2026-09-29 | README and code read from a local clone; AGPL-3.0 |
| francescobelli2003-art/bellomberg | 2026-09-29 | README and code read from a local clone; Apache 2.0 |
| morid648/financial-research-analyst-agent | 2026-09-29 | README and code read from a local clone; MIT |
| lit26/finvizfinance | 2026-09-29 | README and code read from a local clone; MIT |
| FernandoAbishai/ai-value-investing-agents | 2026-09-29 | README and code read from a local clone; licence unconfirmed |
| JerBouma/FinanceToolkit | 2026-09-29 | README and code read from a local clone; MIT |
| zjy1346/OpenThesis (v2.1.0) | 2026-09-29 | README, specs and code read from a local clone; Apache 2.0 |
| zvtvz/zvt | 2026-09-29 | README and code read from a local clone; MIT |
| mnshah3/zen | 2026-09-29 | README and code read from a local clone; no licence file (ideas only) |
| faizancodes/Automated-Fundamental-Analysis | 2026-09-29 | README and code read from a local clone; no licence file (ideas only) |
| devfinwiz/Stock_Screeners_Raw | 2026-09-29 | README and code read from a local clone; GPL-3.0 (ideas only) |
| foolcage/fooltrader | 2026-09-29 | README, design docs and code read from a local clone; MIT |
| JerBouma/FundamentalsQuantifier | 2026-09-29 | README and code read from a local clone; GPL-3.0 (ideas only) |
| JerBouma/FinanceDatabase | 2026-09-29 | README, validation code and tests read from a local clone; MIT (data licence unconfirmed) |
| xbtlin/ai-berkshire | 2026-09-29 | README, skills and tools read from a local clone; MIT (the source of ai-value-investing-agents) |
| quant-sentiment-ai/claude-equity-research | 2026-09-29 | README, command prompt, docs, config and sample reports read from a local clone; MIT |
| chm020924/StockAnalysisSystem | 2026-09-29 | README and all code read from a local clone; no licence file (ideas only) |

## Repositories still to review

_(add here)_

## Candidates

### From TradingAgents

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| T1 | Replay-harness scoring: each action scored by realised alpha against a benchmark (Nifty500 Multicap 50:25:25), grouped by trigger category, on a held-out later period. Sweeps over a ticker × date grid, resumable | Replay harness, Validation | Fine: outcomes are used to measure, never fed to a forward prompt | Before decision support | candidate |
| T2 | Checkpoint/resume for long extraction runs, as Postgres job rows keyed by `content_hash` + extractor version | Guidance extraction over ~100 companies | Fine: no new service | MVP step 3 | candidate |
| T3 | Bull/bear passes emitting falsifiable conditions (metric, operator, threshold, `observe_on`) into `thesis_condition` | `narrate/`, thesis stores | R3: the debate only generates hypotheses; code checks them. No synthesised verdict | After `thesis` has data | candidate |
| T4 | Decision log with realised-outcome fields appended later, computed by code against bear/base/bull | `position_review`, thesis update log | **R2 risk:** outcome or "lesson" text must never reach a forward-looking prompt. Display and replay only | After `position_review` has data | candidate |
| T5 | Holdings passed into the narration step | `portfolio_risk_snapshot`, `narrate/` | Fine: holdings come from adapter code, read-only | After MVP | candidate |
| T6 | Per-task model tiers (cheap for mention extraction, stronger for concall Q&A) set in `EQUITY_*` config | `gateway/` | Fine; consistent with "failing validation reverts the tier" | With first extraction | candidate |
| T7 | Provider-agnostic gateway checklist (Bedrock, Ollama for laptop runs) | `gateway/` | Fine: gateway is the only SDK importer | Low priority | candidate |
| T8 | FRED as a possible global-macro input to `force` | `force`, `market_flow` | Confirm source class and licence first; exposure matrix stays hardcoded | Later | candidate |

### From portfolio-risk-engine (Varense)

No licence in the repo: take ideas and standard formulas only, copy no code.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| P1 | Pure-math risk module, no HTTP or I/O, tested alone | `core/compute/` | Already the convention; confirms the layout | With `portfolio_risk_snapshot` | candidate |
| P2 | Portfolio VaR and CVaR at 95% and 99%, plus max drawdown and annualised volatility, per weekly snapshot | `portfolio_risk_snapshot` | R1: code only. Use adjusted closes known at `t`. Prefer historical simulation first | With `portfolio_risk_snapshot` | candidate |
| P3 | Correlation-aware simulation (Cholesky of the covariance matrix) for bear/base/bull spread | Decision support, scenario valuation | Use only as a stated, seeded, stored-assumption computation. Never as a price forecast | After MVP | candidate |
| P4 | Diversification score from average pairwise correlation, per rolling window | `portfolio_risk_snapshot`, concentration check | R1. Extend with the signed exposure matrix and `relationship_edge` groups (same-group holdings count as one bet) | After MVP | candidate |
| P5 | Beta and downside-risk ratios (Sortino, Jensen's alpha) against a Nifty benchmark | Snapshot, replay scoring (T1) | Use the Nifty 500 Multicap 50:25:25 benchmark. Risk-free rate is a stored, dated assumption | After MVP | candidate |
| P6 | Historical stress replay on Indian episodes (2008, 2013 taper, 2020 COVID, 2022 rate shock) against current holdings | Snapshot | Fixed, hardcoded windows with `rule_version`; this is replay of the past, not look-ahead | After MVP | candidate |
| P7 | Geometric (CAGR-style) annualisation, kept consistent between realised and simulated figures | `core/compute/` | Property-test it | With P2 | candidate |
| P8 | Fail-open cache: an optional cache must never make the app unavailable | Adapters | No Redis (rejected service). Applies as a principle to Postgres-backed caching | Low priority | candidate |
| P9 | Retry and rate-limit handling in the price adapter | `ingest/` Upstox adapter | Already required (throttle, config-driven limits) | With Upstox adapter | candidate |

Not adopted: efficient-frontier optimisation and max-Sharpe portfolios (outputs
target weights, which is a call the project avoids and is overfit-prone);
S&P 500 benchmark; Finnhub/Twelve Data feeds; Supabase auth and a React
frontend (out of scope).

### From Stock_Analysis_Project (alfwro13)

Licence is AGPL-3.0 (copyleft): take ideas and published formulas only, copy
no code. Piotroski, Altman and Beneish are published academic formulas, so
implementing them from the papers is fine.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| S1 | Piotroski F-Score (9 binary tests) computed from `financial_facts`, stored as a computed ratio per filing | `financial_facts`, "fundamental upgrade sustained over N filings" trigger | R1: pure arithmetic from XBRL. Property-test each test. Definitions differ for banks/NBFCs/insurers, so apply per sector with a `rule_version` and mark "not applicable" rather than force a value | After step 1 (facts match Screener) | candidate |
| S2 | Beneish M-Score (earnings-manipulation screen) and Altman Z-Score (distress) as forensic flags | `company_event` red flags, severity by code | R1. Altman and Beneish are built for non-financial manufacturers: exclude financials explicitly. Flag is a lead needing evidence, and `company_event` rows need an `evidence_url` | After step 1 | candidate |
| S3 | Shadow mode for new rules: a rule runs and logs what it would have raised, but alerts nothing until replay on a later period supports it | Trigger lifecycle, replay harness | Supports "tuning only on one period, measure on a later one". Store `rule_version` and status `SHADOW` / `ACTIVE` | With first trigger | candidate |
| S4 | Live tracking of signal precision: each fired signal is later scored against what happened | `technical_signal`, `watchlist_entry` (`EXPIRED` vs `PROMOTED`) | R2: the score is outcome data, display and replay only, never fed to a prompt | With replay harness (T1) | candidate |
| S5 | Signal confluence flag when independent categories align | `ADD_REVIEW` gates | Already required (two independent categories) | Already designed | candidate |
| S6 | Historical-simulation VaR/CVaR at 95% and crisis stress replay | `portfolio_risk_snapshot` | Same as P2, P6 | After MVP | candidate |
| S7 | Post-earnings drift and earnings-volatility profile per company | `scheduled_event` outcomes | R1, R2: computed from prices after the results `as_of`; outcome data, display and replay only | After MVP | candidate |
| S8 | Market regime label (bull / chop / crash) from a deterministic rule | Context for signals, not a trigger | A rule-based label with a `rule_version` is fine. An HMM is a model output: treat as a stored hypothesis that triggers nothing until replay shows value | Later | candidate |
| S9 | Package-drift check on startup and Dependabot pinned updates | CI, `requirements.txt` | Fits the pinned `requirements.txt` rule. Startup check must not run migrations | Low priority | candidate |
| S10 | Vendored front-end libraries, no CDN | Frontend, later | Only if a frontend is ever in scope | Deferred | candidate |

Not adopted: ML ensemble price prediction (XGBoost/Random Forest, a model
deciding a forward view); FinBERT headline scores as anything but a stored
hypothesis; the candlestick and chart-pattern zoo; Bubble Radar composite
(opaque blend of euphoria metrics, hard to validate); meta-labeling that lets
a model veto signals (a model overriding code, against R3); Yahoo Finance,
Fear & Greed and Ghostfolio sync; portfolio optimiser and max-Sharpe weights;
SQLite/Parquet storage; Leitner-box learning mode.

### From bellomberg (francescobelli2003-art)

Apache 2.0: code may be reused with attribution (`THIRD_PARTY_NOTICES`), but
the ideas below are design-level, so none is copied.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| B1 | Red-team role: a separate pass that argues against a memo or thesis before it is stored | `narrate/`, thesis stores | Same as T3: output must be falsifiable conditions checked by code (R3), never a verdict | After `thesis` has data | candidate |
| B2 | Versioned research memos plus a decision journal: rationale recorded at decision time, feedback added later | `thesis` update log, `position_review` | Append-only, `as_of` on every entry. Later feedback is a new row, never an edit (R2). Journal text is outcome-adjacent: not fed to forward prompts | After `position_mandate`/`position_review` exist | candidate |
| B3 | Mandate page: the user's stated rules (max weight, drawdown limit, horizon) stored as data and checked against holdings | Sizing parameters, concentration check | R1: code checks the mandate. Store versioned with `as_of` | With decision support | candidate |
| B4 | Agent-run transparency: per-call log of model, tokens, cost, timing, tools used | `gateway/` | Fits the gateway rule. Store `model_version` already required. Add cost and latency columns | With first extraction | candidate |
| B5 | Performance accounting: time-weighted return, attribution, factor and concentration views | `portfolio_risk_snapshot` | R1: pure functions in `core/compute/`, property-tested. Cash-flow-adjusted TWR needs dated flows | After MVP | candidate |
| B6 | Command-palette / CLI navigation of many views | CLI timeline report | Frontend is out of scope for now | Deferred | candidate |
| B7 | Local-first: data stays on the user's machine, only named requests leave | Deployment, privacy | Already true (family use, private reports). Worth documenting which fields ever go to a model provider | Doc only | candidate |

Not adopted: six-desk AI committee with a synthesis role (agent orchestration
before data, and synthesis is a model deciding); options Greeks, volatility
surfaces and multi-leg simulation (out of scope for a buy-and-hold equity
system); crypto desk; OpenRouter as a gateway (provider choice stays inside
`gateway/`); Electron UI; SQLite; bilingual UI.

### From financial-research-analyst-agent (morid648)

MIT: code may be copied with the notice kept in the module and in
`THIRD_PARTY_NOTICES`. The ideas below are design-level, so none is copied.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| F1 | Explicit `N/A` when inputs are missing, never a silent default. Each ratio carries a data-availability confidence | `financial_facts` computed ratios | R1. Return a typed "not computable" with the missing input named, so a gap can't be read as zero. Test that missing inputs never produce a number | Step 1 | candidate |
| F2 | Documented root-cause audit of bugs found (silently zeroed DCF, hardcoded strings, unit errors) | Testing, review checklist | Turn the failure classes into property tests: unit scale (lakh/crore, per-share vs total), zero-as-missing, hardcoded constants | Step 1 | candidate |
| F3 | Formula glossary page: each ratio's definition shown next to the number | Reports | Generate from the same definition the code uses, so docs can't drift. Carry the disclaimer wherever a baseline appears | After MVP | candidate |
| F4 | DCF with WACC via CAPM and a bear/base/bull sensitivity grid | Decision support, scenario valuation | R1: pure functions. Every assumption stored with the assumption-set hash. Use reverse DCF (what the price implies) as the primary view; a forward DCF only on stated assumptions | After MVP | candidate |
| F5 | Swappable provider abstraction over one interface | Adapters | Already required (one adapter per source, config-driven). Confirms the pattern | Already designed | candidate |
| F6 | Tools as standalone functions, with the API layer just calling them | `core/compute/`, `core/db/pit.py` | Matches the layout. Also the shape needed for the later read-only MCP server | Already designed | candidate |
| F7 | Non-agentic core: deterministic pages work with no LLM at all; agent layer optional and on top | Architecture | Same principle as R1. Keep the stores usable with the LLM switched off | Already designed | candidate |
| F8 | De-engineering pass: periodic removal of dead code with regression checks | Maintenance | Tie to the architecture test so removed modules can't leave dangling rules | Later | candidate |

Not adopted: yfinance and the alternate providers (FMP, Alpha Vantage,
OpenBB) as sources, since the source table is exchange filings plus Upstox;
VADER and FinBERT scores as anything but a stored hypothesis; rule-based
"signal" on the overview page (no `rule_version` or replay behind it);
LangChain/LangGraph/ChromaDB agent layer (agent orchestration and a separate
vector DB, both on the push-back list); RAG over filings for numbers (R1);
Chart.js frontend.

### From finvizfinance (lit26)

MIT. A scraper for FinViz, which covers US stocks, forex, crypto and futures.
Almost nothing transfers: the data is US-centric and the method is scraping a
third-party site with no exchange filing behind it.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| V1 | Screener filter vocabulary (sector, valuation, technical signal buckets) as a checklist of screen dimensions | Future screens over `financial_facts` | R1: filters run in SQL over our own stores, with parameters under a `rule_version`. Tune on one period only | After MVP | candidate |
| V2 | Economic-calendar and earnings-date views as a display shape | `scheduled_event` timeline | Our source is exchange announcements, not a scraped calendar. Forecast fields must not be treated as known outcomes (R2) | After MVP | candidate |
| V3 | Optional proxy support in the client | none | Rejected: rotating proxies are on the "never escalate" list | Rejected | rejected |

Not adopted: FinViz as a source (unofficial scraping, US-centric, no NSE/BSE
coverage, an extra `web_scrape` adapter with no filing evidence); forex,
crypto and futures data; its insider-trading table (our `insider_trade` comes
from NSE/BSE disclosures with a mode-of-acquisition field).

### From ai-value-investing-agents (FernandoAbishai)

Licence not confirmed from the README (MIT inferred): check the LICENSE file
before copying anything. Ideas only for now. Its skills are Claude Code
workflow files, not a data system, so the transferable parts are the
discipline and the checks.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| A1 | Source and calculation register: every report lists each number's source document and the calculation that produced it | Reports, `narrate/` | Fits R1 ("every number traces to a parser or a computation"). Render from stored provenance columns, not model text | After MVP | candidate |
| A2 | Uncertainty labels and explicit decision gates on outputs | Reports, decision support | Labels computed by code (e.g. data completeness, assumption count), never model-asserted. Gates already exist for `ADD_REVIEW` | After MVP | candidate |
| A3 | Exact-decimal verification tool: currency, share count, unit and date consistency checks on inputs | `core/compute/` | R1, and matches "Money as `Decimal`". Add checks for units (crore vs million) and share-count basis after splits and bonuses | Step 1 | candidate |
| A4 | Terminal-value/IRR analysis that keeps discrete risks out of the discount rate | Scenario valuation | R1: pure function. Risks become explicit scenarios (bear/base/bull), not a bumped WACC. Assumptions stored with the hash | After MVP | candidate |
| A5 | Benford's Law check on reported figures as a data-quality and manipulation lead | `company_event` red flags, ingest validation | R1: statistic computed by code. Weak on small samples, so a lead only, needs evidence, and may not raise severity alone | Later | candidate |
| A6 | Thesis-drift tracking: compare how the stated rationale changes across filings and calls | `thesis` update log, `guidance_claim` | R3: drift is measured over stored structured claims and conditions by code, not by asking a model whether the story changed | After `thesis` has data | candidate |
| A7 | Falsifiable holding and exit signals required on every thesis | `thesis_condition` | Already required (metric, operator, threshold, `observe_on`) | Already designed | candidate |
| A8 | Cross-validation of financial data against a second source | Screener validation, XBRL vs price adapter | Already planned for the 20-company sample. Keep it as an ongoing canary, not one-off | Step 1 | candidate |
| A9 | Generated-file drift detection in CI | CI, `docs/` | Useful if any doc or config is generated from code (e.g. F3 glossary). Fail CI when the generated copy differs | Later | candidate |
| A10 | Quality filter and bottleneck detection as screening dimensions (industry mapping, secular growth) | Future screens, `sub_index` | R1: hand-curated `sub_index` constituents, never model-assigned. Screens over `financial_facts` only | After MVP | candidate |

Deferred by request, to be reviewed later (moved out of "not adopted"). None
is planned, and each has constraints to settle first:

| # | Idea | Constraints to settle when reviewed | Status |
|---|---|---|---|
| A11 | Investor personas and "team" synthesis: several analytical perspectives (Buffett, Munger, Li Lu style) run in parallel and merged | Agent orchestration is on the push-back list until the stores have data, so this waits for populated stores. A persona must produce only falsifiable conditions and prose framing, never numbers (R1) or a stored verdict (R3). The synthesis step cannot pick or override code-computed severity, and persona output is a hypothesis with `model_version`. Overlaps T3 and B1 (bull/bear and red-team passes), so decide them together. Each persona gets a per-step tool allowlist with no order-capable tool | deferred |
| A12 | Management-quality review and private-company research | Management review must draw on stored `guidance_claim` delivery rates and `company_event` rows, which are code-computed, with the model only narrating them. Private-company research has no exchange filings to serve as evidence, so it conflicts with "every row needs `evidence_url`". Decide whether it is in scope at all, and if so under what source class | deferred |

**Direction given (2026-09-29): follow the source repo's approach**, meaning
the model performs the persona analysis, the synthesis and the management
review, as ai-value-investing-agents does. This is recorded as intent for the
later review. It does not lift the constraints in the table above, which come
from CLAUDE.md and stay in force when this is planned:

- The model's output is a stored hypothesis with `model_version` and attached
  falsifiable conditions, never a stored conclusion (R3).
- No number comes from the model. Figures in its prose come from parsers and
  computations and are cited from the register (A1) (R1).
- Data given to it is filtered `as_of` ≤ `t`, with no outcome data in prompts
  that make forward-looking judgements (R2).
- Filings, transcripts and web pages are untrusted input; prompts say so.
- No agent has an order-capable tool.
- It starts only once the stores hold data (after MVP step 4), and this note
  should be reread then. If a CLAUDE.md rule needs changing to allow the repo's
  approach, change the rule first, in its own edit.

**Priority note (2026-09-29): low.** AI summaries of concalls and financials
are already everywhere (concall.ai, screener.in), so a general summary is not
what this system needs to build. Anything here earns its place only where it
does what those tools do not: point-in-time (`as_of`) reads, code-resolved
guidance delivery rates and `SILENT` detection, falsifiable conditions checked
later, and provenance for every number. Where a plain summary is enough, use
those tools directly (interactive lookups only, not ingested: numbers never
enter the stores through a model or a third-party summary, R1). A11 and A12
stay deferred and are dropped if the stores' own outputs prove sufficient.

Decision point for both: after step 4 of the MVP (delivery rate per company)
has real data, since management review depends on it.

Not adopted: article and memo publishing (reports are private to the family);
the parallel Codex packaging and install-sync scripts; "no hallucination gates,
rely on source validation" as a stance, since we go further by keeping numbers
out of the model entirely.

### From FinanceToolkit (JerBouma)

MIT: code may be copied with the notice kept in the module and in
`THIRD_PARTY_NOTICES`. Best used as a **formula reference and test oracle**
for `core/compute/`, since it is a library of 500+ transparent calculations.
Its inputs come from FinancialModelingPrep and Yahoo, not XBRL, so it is not a
data source for us.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| K1 | Ratio catalogue (efficiency, liquidity, profitability, solvency, valuation) as a checklist of what `financial_facts` should compute | `financial_facts` computed ratios | R1: implement each as a pure function from XBRL line items. Record the definition used, since the point of the library is that definitions differ by provider. Pin ours in code under a `rule_version` | Step 1 | candidate |
| K2 | Use it as an independent oracle: feed the same inputs to both and compare outputs in tests | Property and contract tests | Dev-only dependency, never imported by production code. Pin the version in `requirements.txt` | Step 1 | candidate |
| K3 | DuPont and extended DuPont decomposition of ROE | `financial_facts` durability metrics | R1: pure arithmetic. Useful for quality-of-returns view (margin vs turnover vs leverage) | After step 1 | candidate |
| K4 | EVA and ROIC vs WACC spread | Durability metrics, valuation | R1. WACC inputs (risk-free, beta, ERP) are stored dated assumptions | After MVP | candidate |
| K5 | Altman Z and Beneish M implementations | S2 (forensic flags) | Cross-check our implementation against it. Same caveat: not for financials | After step 1 | candidate |
| K6 | Graham Number as a simple value cross-check | Valuation, display only | R1. A cross-check, not a target, and not a source of `ADD_REVIEW` | Later | candidate |
| K7 | Calmar ratio, EWMA volatility, Sortino as extra risk/performance metrics | `portfolio_risk_snapshot` | R1: pure functions. Adds to P2 and P5 | After MVP | candidate |
| K8 | Calendar-period normalisation for different fiscal years, growth/lag/rolling/trailing as generic operators | `financial_facts` | Indian filers mostly use April–March, but some differ (e.g. subsidiaries of foreign groups). Normalise in code with the period end stored. TTM must use only filings with `as_of` ≤ `t` (R2) | Step 1 | candidate |
| K9 | Z-score standardisation across peers | Peer comparison | Peer set is hand-curated, never model-picked (R1). Compute in code | Later | candidate |
| K10 | MCP server exposing calculations to Claude | Future read-only MCP server | Confirms the planned server shape: tools wrap point-in-time functions | After MVP | candidate |
| K11 | Cache that never auto-clears and updates incrementally | Adapters, blob storage | Matches write-once, keyed by `content_hash`. A cache is not a substitute for `as_of` | Already designed | candidate |

Reports and visuals (added after a follow-up check): the library has no UI of
its own. It shows results as Jupyter notebook plots, and a companion project,
FinanceDashboard, is mentioned but not yet reviewed. Frontend is out of scope
per CLAUDE.md, so these are display-shape ideas for the later CLI report or
frontend.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| K12 | Chart set worth reproducing: extended DuPont breakdown, profitability ratio trends, cumulative returns, VaR trend, allocation and return-distribution views, economic indicator comparisons | CLI timeline report, later frontend | Charts plot code-computed series read through `pit.py` with `as_of` ≤ `t`. Any baseline or stop shown carries the disclaimer, with a test that fails if omitted | Deferred until a frontend is in scope | candidate |
| K13 | Review the companion FinanceDashboard repo for its UI layout | Later frontend | Not yet reviewed: send the link if wanted | Pending | candidate |
| K14 | Excel/CSV export of any table | Reports | Family use only. Export must carry `as_of` and the disclaimer | Later | candidate |

Not adopted: FinancialModelingPrep and Yahoo as sources (source table is
exchange filings and Upstox); options pricing and Greeks, fixed income and
bond valuation (out of scope); the econometrics module and ARIMA forecasting
(model-derived forward view, and overfitting risk); 40+ technical indicators
(our `technical_signal` rules are few, versioned and replay-validated; add none
without that); Fama-French factors (no Indian factor data source yet); its
SQLite cache; the `enforce_source` fallback from one provider to another
(we substitute nothing when a source is disabled).

## Deep-review additions (2026-09-29)

Second pass over local shallow clones (read-only, deleted afterwards). Ideas the
README-only pass missed. All `candidate`, all after the MVP unless noted. Each
passes R1-R3 as worded.

### TradingAgents, additional

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| T9 | Distinguish "absent" from "window not observed" (its `coverage_gap`): a source outage must not read as silence | `SILENT` detection in `guidance_claim`, canary results | R1, R2: code decides; a claim is `SILENT` only if the transcript for that period was actually ingested | candidate |
| T10 | Half-open date windows in one timezone; undated items excluded from a point-in-time run | `pit.py`, news and announcement reads | R2: an undated item cannot be proven known at `t` | candidate |
| T11 | Outcome-known date on each decision-log entry; as-of reads exclude lessons whose outcome was unknown at `t` | T4, `position_review` | R2: prevents outcome leakage into later prompts | candidate |
| T12 | Typed adapter errors (no-data, rate-limited, ticker-not-found, schema-changed) plus an AST layering test | `ingest/`, `tests/test_architecture.py` | Extends the existing architecture test | candidate |
| T13 | Explicit NO_DATA sentinel and stale-data guard; never cache a failure | Adapters, blob storage | Consistent with write-once objects keyed by `content_hash` | candidate |
| T14 | An unparsable model decision defaults to the safe outcome (review, never action) | `narrate/`, `gateway/` | R3 | candidate |
| T15 | Prompt-integrity tests (untrusted-input warning present in every prompt) | `extract/`, `narrate/` tests | Enforces "documents are untrusted input" | candidate |
| T16 | Backtest scores decisions only and refuses to become an execution simulator | Replay harness | Matches "never execution" | candidate |
| T17 | Explicit handling of a model `stop_reason == "refusal"` | `gateway/` | Return a typed failure; never retry with a different prompt | candidate |

### bellomberg, additional

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| B8 | Sector valuation-method registry with input contracts (banks, NBFCs, insurers each get their own method and required inputs) | Scenario valuation, `financial_facts` | R1: hardcoded table under `rule_version`; a missing input yields N/A | candidate |
| B9 | Deterministic memo linter over model prose: flags numbers not traceable to a stored value, flag-only | `narrate/` output checks | R1: enforces "every number traces to a parser or computation" | candidate |
| B10 | Freshness detector: identical values across periods flagged as possibly stale | Ingest validation | R1, code only | candidate |
| B11 | Flag-only action validator quoting the user's stated mandate verbatim | B3, decision support | Flags; never blocks or sells | candidate |
| B12 | Kupiec and Christoffersen backtests for VaR | P2 validation | R1: pure statistics | candidate |
| B13 | Scorekeeper that injects past track record into forward prompts | none | R2: outcome data reaching a forward-looking prompt | rejected |

### Stock_Analysis_Project, additional (AGPL: ideas only)

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| S11 | Pillar confluence: abstain on conflict rather than average | `ADD_REVIEW` gates | Close to the two-category gate; adds explicit "conflict = no flag" | candidate |
| S12 | Shadow-mode referee comparing a new rule with the live one (extends S3) | Trigger validation | R2: replay on a later period | candidate |
| S13 | "Pending until resolved" forward-return tracker | S4, `watchlist_entry` | Outcome computed by code after the window closes | candidate |
| S14 | Portfolio heat index with configurable thresholds | `portfolio_risk_snapshot` | R1; thresholds hardcoded under `rule_version` | candidate |

### financial-research-analyst-agent, additional (MIT)

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| F9 | Its root-cause audit lists failure classes to test for: unit-ambiguous fields (100x errors), hardcoded placeholder ratios, a swallowed `except: pass` returning a plausible wrong number, a client re-implementing a server formula | Test checklist | Add tests: no bare `except`, one formula implementation per ratio | candidate |
| F10 | Data-quality validator run on every provider response before analysis (staleness, missing fields, suspicious values) | `ingest/` validation | R1; extends strict Pydantic models with plausibility bounds | candidate |
| F11 | Damodaran-style FCFF engine with dated country-risk and industry tables stored with source and `as_of` | K4, scenario valuation | R1; tables are dated stored assumptions; check Damodaran's data reuse terms; India ERP and country risk apply | candidate |
| F12 | Capitalised R&D converter as an optional adjustment | Valuation | Low relevance for the Nifty 100 | deferred |

### portfolio-risk-engine, additional (no licence: ideas only)

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| P10 | Efficient-frontier simulation as an allocation view | `portfolio_risk_snapshot` | R1; display only, never a target | candidate |
| P11 | Rolling metrics, Treynor and Information ratio | Snapshot | R1: pure functions | candidate |
| P12 | Metric tooltips, "why this matters" blocks and tiered Learn content (plain, investor, expert) | Reports, later frontend | Definitions generated from the code's own docstrings (F3) | deferred |
| P13 | Methodology page checked line by line against the code | Docs, tests | Make it a test that formula docs match the code | candidate |
| P14 | PDF and CSV export of a portfolio report | K14 | Must carry `as_of` and the disclaimer; a test fails if absent | candidate |
| P15 | Separate "ticker not found" from "rate limited" | T12 | Typed adapter errors | merged into T12 |

### ai-value-investing-agents, additional (licence unconfirmed: ideas only)

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| A13 | Seven-metric quality screen with hard cut-offs and exemption rules; banks and insurers exempt from interest coverage (10-yr ROE, 5-yr FCF, coverage, gross margin, OCF/NI, net margin, share-count growth) | `financial_facts` screens | R1: thresholds hardcoded under `rule_version`; tune on one period, measure on a later one | candidate |
| A14 | Report audit: extract data points from a report, sample 15% with a fixed seed, compare against a second source, pass or return | Report tests, A8 | R1: sampling and comparison by code | candidate |
| A15 | Three-way drift classification: evidence changed vs price changed vs wording changed; only evidence counts | A6, `thesis` update log | R3: a model's wording change must raise nothing | candidate |
| A16 | Thesis written as five fixed questions in under 200 words, with red lines and valuation anchors | `thesis` schema | Pydantic schema; conditions stored as data | candidate |
| A17 | Three-scenario valuation and exit P/E from ROIC and growth | F4, scenario valuation | R1: pure functions; assumptions stored with hash | candidate |
| A18 | Prompt rules: state the cutoff date, mark fact vs estimate vs judgement, do not impersonate real investors | `narrate/` prompts | Fits the prompt-integrity tests (T15) | candidate |

### FinanceToolkit, additional (MIT)

| # | Idea | Where it fits | Rule check | Status |
|---|---|---|---|---|
| K15 | Full model set: Piotroski, Beneish, Altman, Ohlson, Zmijewski, Springate, Fulmer, Grover, DuPont, EVA, WACC, intrinsic value | S1, S2, K3-K5 | R1. Distress scores are not valid for banks and NBFCs | candidate |
| K16 | Extra ratios for the catalogue: cash tax rate and tax-rate divergence, SBC-adjusted FCF, buyback and shareholder yield, capex and dividend coverage, defensive interval, net current asset value | K1, `financial_facts` | R1 | candidate |
| K17 | Risk suite: EVaR, CoVaR, GARCH, copula, realised volatility; VaR backtesting | P2, B12 | R1; VaR and CVaR first, the rest only if measured to matter | deferred |
| K18 | Performance suite: capture ratios, Omega, downside deviation | K7 | R1 | candidate |
| K19 | Recorded-output regression tests: results stored as CSV and compared with tolerance (last digit, float noise, signed zero, infinity; text exact) | Testing | Suits property tests on financial computations | candidate |
| K20 | MCP server layout: registry, argument coercion, result formatting, auth middleware | Future read-only MCP server | Reference only; ours wraps `pit.py` reads | candidate |
| K21 | Econometrics (event study, Fama-MacBeth, cointegration, causality) | Replay analysis of index and ownership events | R1: statistics in code; replay only, never a trigger | deferred |
| K22 | Options and fixed-income modules | none | Out of scope for buy-and-hold equities | rejected |

### From OpenThesis (zjy1346)

Apache 2.0: code may be copied with notice. A Windows desktop app (Tauri +
Python sidecar) for US, China A-share and HK filings; the pipeline is filings →
validated facts → deterministic finance → model agents → verification. Closest
match to our R1 so far, so the ideas are mostly about the ingestion and
failure-handling layers. Read: README, specs, `financial_recovery`,
`financial_checkpoint`, `research_readiness`, `market_snapshot`, report
projection, thesis lineage, architecture and FX tests, research notes.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| O1 | Fail-closed ingestion: no model is called until identity, period, unit and statement-reconciliation checks pass; a missing value is never treated as zero; each fact keeps period, consolidation scope, currency, unit, page and evidence id | `financial_facts`, XBRL parser, before Screener validation | R1. Reconciliation (assets = liabilities + equity, cash-flow tie-out, subtotals) as pure functions in `core/compute/` | MVP step 1 | candidate |
| O2 | Terminal recovery states for a job: `RESOLVED`, `RESOLVED_STALE`, `NEEDS_CONFIGURATION`, `RETRYABLE_EXTERNAL_FAILURE`, `EXHAUSTED_SAME_INPUT`, `BLOCKED_INTEGRITY`, `CANCELLED`. Same input plus same parser version is never retried forever | Ingest job rows, canary, T2 | R1. Extends "disabled, not failed" with more explicit states | With T2 | candidate |
| O3 | Content-addressed checkpoint key = document hash + parser version + rules version + schema version; a parser fix re-parses only what its key invalidates | Blob storage, re-parse from stored bytes | Matches write-once objects keyed by `content_hash` | With T2 | candidate |
| O4 | Coverage gate before a company enters research: N consecutive verified consolidated full-year filings covering revenue, assets, liabilities, equity in the reporting currency, with no issues | Universe onboarding, sample selection, `ADD_REVIEW` thesis-intact gate | R1, R2: computed from stored facts with `as_of <= t` | After MVP step 1 | candidate |
| O5 | Typed report projection: the only path from stored data to a report is a whitelist of sections; unknown keys are dropped and logged; technical ids stay technical-only; partial or failed runs say so with a reason | `narrate/`, report renderer, disclaimer test | R1, R3. The disclaimer test can sit on the projection layer | Later | candidate |
| O6 | Bounded repair: one re-synthesis call from saved stage artifacts, earlier stages never rerun; authentication, quota and rate-limit failures are terminal; a second invalid output leaves the run `PARTIAL` | `gateway/`, `narrate/` | R3. Bounded retries and no silent fallback | With `narrate/` | candidate |
| O7 | Gateway never silently switches to another (paid or different) model after a failure; each run records model reference, parameters and configuration version | `gateway/`, `model_version` provenance | R2: a silent switch would make replay unreproducible | With `gateway/` | candidate |
| O8 | Run two models on the same curated evidence and compare (its primary plus comparison models); for us, disagreement on a guidance extraction goes to quarantine | `extract/`, Screener validation | R3: agreement is evidence, not proof; numbers still come from parsers | With extraction | candidate |
| O9 | Thesis versions carry a parent id and source evidence; a save against another company's parent is rejected | `thesis` append-only update log | R2, R3. Lineage without editing history | With `thesis` | candidate |
| O10 | Download safety: caps on file count, size and compression ratio; rejects path traversal, absolute paths, symlinks and case-colliding names | `ingest/` archive handling (XBRL zips, filings) | Security hygiene for untrusted files | With ingest | candidate |
| O11 | Declarative, signed alias/compatibility packs: data only (no code, paths or URLs), canonical hash and Ed25519 signature verified before use | Alias table for news mentions, XBRL tag aliases | Fits the human-curated alias table with dated validity. Signing is optional for family use | Later | candidate |
| O12 | Context handling rule: authoritative facts are never compressed or summarised; if the context is too small, split the call or stop; semantic prompt compression only on non-authoritative narrative | `gateway/`, `extract/` | R1. "Reversible by hash" is not "the model saw it" | With extraction | candidate |
| O13 | Golden corpus and benchmark script for financial recognition, kept in CI so a parser change cannot lower accuracy silently | XBRL parser, Screener validation sample | Matches "a failing validation reverts, not lowers the threshold" | MVP step 1 | candidate |
| O14 | Cross-rate FX: use the latest common observation date, record its `as_of`, return nothing rather than invent a rate; listing currency and reporting currency stay separate | Filings reported in USD, adjusted comparisons | R1, R2. Only if a foreign-currency reporter appears | Deferred | candidate |
| O15 | Key handling: keys in the OS credential store, a new key tested as a staged version before an atomic switch, never in logs or stored files | `gateway/`, secrets | Deployment uses Secrets/ConfigMaps; keep the "test before switch" step | Deployment | candidate |
| O16 | Financial institutions marked Beta: standard free-cash-flow reverse DCF is not applied to banks or insurers | B8 method registry | Confirms the sector-method table | With B8 | merged into B8 |

Not adopted:

- Visual (vision-model) extraction of numbers from scanned reports: R1 forbids
  extracting a number through a model. Fall back to the manual drop folder.
- User-typed prices as the market snapshot: prices come from Upstox.
- Windows desktop packaging (Tauri, sidecar, installer) and its frontend: no
  frontend in scope.
- `.ot` research packs as a distribution format: workflow logic stays in code
  under `rule_version`; only the hashing and least-privilege ideas are kept
  (O10, O11).
- Model "specialist agents" producing financial, growth and accounting-risk
  judgements: R1 and no agent orchestration yet.

### From zvt (zvtvz)

MIT: code may be copied with notice. A Chinese-market quant framework: a
schema/recorder/provider data layer, factors, a simulated trader, a QMT broker
bridge, a tag system with LLM suggestions, Dash and REST front ends. Mostly a
trading platform, so most of it is out of scope; the data-layer and tag-review
patterns are the useful part. Read: README, `contract/` (schema, recorder,
state), `domain/` (finance, holder, news), `tag/`, `ml/`, `trader/`, tests.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| Z1 | Recorder state rows (`recorder_state`, `tagger_state`, `factor_state`): each incremental job stores its own resume point, so a rerun starts where it stopped | Ingest jobs, T2, O2 | R2: the resume point is a fetch cursor, never a rewrite of history. Keep it in Postgres, not files | With T2 | merged into T2 |
| Z2 | One schema served by several providers, with a `provider` column on each row and a sample-based `test_data_correctness(provider, samples)` per provider | Provenance (`extracted_by`, source class), contract tests | Matches recorded-response contract tests; add sample rows per adapter | With ingest | candidate |
| Z3 | Exchange calendar model: trading intervals, "in trading time", and "is this bar finished" checks before a daily bar is used | `technical_signal`, price adapter | R2: a signal must never use a bar that was not final at `t`. NSE hours and holidays as dated data | With `technical_signal` | candidate |
| Z4 | Statutory-report tables keep both `report_period` and `report_date` (period end vs publication date), and holder tables carry change and change-ratio against the prior report | `financial_facts`, `shareholding_pattern` | Confirms `as_of` after quarter end; store the change fields as computed, not extracted | Already designed | candidate |
| Z5 | Separate field sets for banks and insurers in the balance sheet schema (deposits, insurance items) | `financial_facts`, B8 | R1: sector-specific line items with their own checks | With B8 | merged into B8 |
| Z6 | Tag suggestions from news held as a suggestion field on the news row, with same-story reuse instead of a second model call | `news_item`, `sub_index` review queue | R1, R3: a suggestion never becomes membership; a human accepts it into the curated table, like quarantine review | With news | candidate |
| Z7 | Market-wide flow tables (money flow) and monetary/macro tables next to stock data | `market_flow`, `force` | R2: `as_of` per row; Indian sources (NSDL, RBI) need a class check | With `market_flow` | candidate |
| Z8 | Notification channel abstraction (`informer`) | Alert delivery (later decision) | Alerts lead the report until delivery is decided | Deferred | candidate |
| Z9 | Service layer plus route registry over schemas, served by REST | Future read-only MCP server (`pit.py`) | Read-only only; no order routes | Later | candidate |

Not adopted:

- Simulated account and trader (`sim_account`, order-by-position-percent) and
  the QMT broker bridge: order-capable, and execution simulation. Same reason
  as the TradingAgents and bellomberg trader rejections.
- The ML price-prediction machine (`MaStockMLMachine`): a model forecasting
  prices breaks R1 and R3. Only replay-validated rules may trigger anything.
- Zen (Chan-theory) and MACD factor libraries: many tunable parameters on the
  full history is the overfitting the project rules forbid.
- Its LLM news step: it filters headlines by price-move words (up, limit-up,
  down), so outcome data steers the model, and it parses the reply with a
  regex and `json.loads` and no schema. Ours needs a fixed enum, a Pydantic
  schema and code-side resolution.
- Chinese-market sources (EastMoney, JoinQuant, Sina, QMT): out of universe;
  they are scrapers of the `web_scrape` class.
- Its "no backward compatibility" stance: our schemas migrate with Alembic.

### From zen (mnshah3)

No licence file: ideas and facts only, no code copied. An Indian-market
research archive (NSE bhavcopy, announcements, XBRL financials, corporate
actions) with a pre-registered momentum/quality strategy, a look-ahead
detector, and a public list of its own mistakes. The closest match to R2 of
any repo reviewed, and the most useful for validation discipline. Read: README
(including its Mistakes table), `zen/validation/leak.py`, `trials.py`,
`zen/universe/identity.py`, `zen/signals/base.py`, `research/strategy/v1-spec.md`,
`state/trials.jsonl`, `jobs/check_parser_drift.py`, `jobs/readme_numbers.py`,
`jobs/update_corpactions.py`, `tests/test_india_tax.py`, the CI workflow.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| Z10 | Truncation-equality look-ahead test: run a rule as of T on the full archive, then on a copy physically truncated at T; the answers must be identical. Truncation is real, not a query filter. Every store is truncated on its own date column, and a test fails if a store exists that the truncation list omits (its own detector once truncated only prices) | `tests/`, replay harness, `pit.py` | R2: catches shifted columns, whole-sample normalisation and joins that pull tomorrow's row, without knowing which. Needs a per-store dating column table kept in step with the schema | With the first computed signal | candidate |
| Z11 | Detector must fail on a known-bad strategy: a test strategy that deliberately reads tomorrow's data must be flagged, so a detector that has gone blind is noticed | Replay harness tests | R2 | With Z10 | candidate |
| Z12 | Pre-registered spec: rules, dates, universe and success bars written and dated before any run; ambiguities resolved in a dated Clarifications section, never by looking at returns; the held-back period is locked in code and refuses to compute returns after the in-sample end without an explicit unlock, run once | Trigger validation, replay harness, `rule_version` | R2, and "tune on one period, measure on another". Extends the rule with a code-enforced lock | With replay harness | candidate |
| Z13 | Trials log: every hypothesis tested is appended, including abandoned ones, and the count is carried into reporting because the best of N tries inflates the winner; anchored to the repo root, not the working directory, so a wrong cwd cannot return zero | Replay harness, `rule_version` | R2, anti-overfitting. Store in Postgres as an append-only table, not a file | With replay harness | candidate |
| Z14 | Signal constructor refuses a signal with no stated case against it (`against` list) or no rationale | `technical_signal`, `watchlist_entry`, `position_review` | R1, R3: the counter-case is data stored on the row, computed from the same rule (for example the gate that was closest to failing), not model prose | With `technical_signal` | candidate |
| Z15 | Calibration baseline: a known factor (momentum) with a roughly known return is wired in first and labelled `CALIBRATION`, "not a strategy for real money"; if it reports an implausible number the harness is broken. Anything unproven carries `validated = false` by default | Replay harness, report labels | R2. Also a natural label for unvalidated rules in reports | With replay harness | candidate |
| Z16 | Corpus-level generated numbers: every figure in the README sits between markers and is rendered from committed outputs, with a checksum check that the inputs still match, and a test that fails on drift | Docs, report tests, P13 | R1: every number traces to a computation. Extends P13 | Later | candidate |
| Z17 | Parser drift check: re-parse a random sample of stored raw documents with the current parser and compare every field, not a chosen subset (its first version omitted two fields and 55 of 56 real differences were in them); tolerance-based | XBRL parser, blob storage, O3, O13 | R1. Works because raw bytes are stored: a parser fix re-parses what is stored | With XBRL parser | candidate |
| Z18 | XBRL segment-context bug: a segment (dimensioned) context shares the parent's period, so one division's revenue can be read as the company's. Pick only the undimensioned context, and cover it with a test | XBRL parser | R1: a known failure of the source of truth | MVP step 1 | candidate |
| Z19 | Stock identity across renames built from (symbol, ISIN) spells: two spells are one stock if they share a symbol or an ISIN and the later starts within 60 sessions of the earlier ending; a third pass links same-issuer ISINs (first 7 characters) when both symbol and ISIN change on one day; a symbol reused later by another company is not merged. Its sizing: 40 to 60 names a decision date are lost if this is done on raw symbols | Entity table, dated symbol to ISIN map | R2: excluding a renamed company depends on an event after the date, itself look-ahead. Our design drives this from `corporate_action`; this is the fallback test set | With entity table | candidate |
| Z20 | Same-day corporate actions: a split and a bonus on one ex-date must both be applied (one factor applied gave a 5x price error); flag any single-session return that cannot be real | Adjustment factors, price cross-check | R1, R2: implausible-return check as code | With `corporate_action` | candidate |
| Z21 | Quarter figure counted only if its period is at most about 100 days; latest quarter's `period_end` within 200 days of the decision date; consolidated versus standalone chosen once per company and held consistent across its quarters; the latest revision known before D is used | `financial_facts`, computed ratios | R2. Guards half-year and cumulative figures being read as a quarter | MVP step 1 | candidate |
| Z22 | Time filter is broadcast time strictly before the decision date; `period_end` is never a time filter; a filing's `as_of` is its broadcast timestamp | `financial_facts`, `pit.py` | R2. Confirms the existing rule; adopt the wording | Already designed | candidate |
| Z23 | Category first-trustworthy-year in code (`USABLE_FROM`): NSE only labelled filing types from 2024-09-23 in one day, so a study reaching before that measures a labelling change; each category has a dated floor and a test that studies apply it | Announcement adapter, `company_event`, `scheduled_event`, replay | R2: a stored provenance fact per source category. Use the exchange's own filing subtype, never regex on free text | With announcement adapter | candidate |
| Z24 | Filing categorisation by regex on free text produced a bucket of 14,955 filings where only about 24% were the intended kind and about 27% were penalties (opposite sign); NSE's own subtype gave 3,220. Also a test where the pattern was matching the label text, not the content (87% claimed recall, 49% real) | `company_event` severity, announcement adapter | R1: classify by the exchange's structured field. Test recall against an independent reader | With announcement adapter | candidate |
| Z25 | Join filings to prices on the last session at or before the date, never on equality (70,506 filings dropped on weekend and holiday dates, skewed to long weekends) | `pit.py`, replay, `technical_signal` | R2, R1 | With price adapter | candidate |
| Z26 | Matched control for event studies: control group matched on liquidity (it was 6.4x more liquid than the event stocks and manufactured an apparent +20pp edge that fell to +6.2pp) | Replay of ownership, index, news events | R2, R1. Any "event reaction" claim must state its control | With replay harness | candidate |
| Z27 | Schema-parity test: schema derived from the data definition must match the migration, because a column added to data but not the CREATE TABLE broke both daily jobs for four days; CI builds from an empty database | Alembic migrations, CI | Matches "every schema change is a migration"; add an autogenerate-diff test | With migrations | candidate |
| Z28 | Job hygiene from its mistakes table: backfill resume must diff written rows against the index (171 filings became permanent holes); throughput monitor for a job that silently slows with no error; time both variants of a fix | Ingest jobs, T2, canary | R1: canary that compares row counts against the source index | With T2 | candidate |
| Z29 | Publish statistics only after an independent second audit: a factor regression subtracted the risk-free rate twice and turned an insignificant alpha (t = 0.99) into a nearly significant one (t = 1.96). Statistics code needs a hand-worked test and an independent check before a conclusion rests on it | K21, replay statistics | R1. Adds worked-example tests for every statistic | With replay harness | candidate |
| Z30 | Indian delivery costs and capital-gains tax model, with every expected figure worked by hand in the test comment: 12-month long-term boundary with leap-day rule, FIFO lots, STT and charges, cess, tax timing; also after-tax and total-return-index (TRI) benchmarks | K7, sizing, portfolio reports | R1: pure functions in `core/compute/`; tax rules are dated (`rule_version` per financial year). "Tax shown as context only" per CLAUDE.md | Later | candidate |
| Z31 | Survivorship-free universe: every listed symbol on each date from NSE bhavcopy archives, including delisted ones, so returns to delisted stocks (worst -99%) are counted; delisted count reported per horizon | Universe, replay | R2. Confirms the dated-membership design; bhavcopy is `official_archive` | With universe | candidate |
| Z32 | Two engines: the same spec implemented twice, the second written without reading the first, must agree on every holding, trade and daily value | Replay harness | R1. Expensive; use for the first trigger that would ever drive a review | Later | candidate |
| Z33 | LLM only rewrites stories whose selection was made by code; claims are worded inside their evidence (no causal claim the data does not carry); charts drawn as plain HTML tables | `narrate/`, reports | R1, R3. Fits B9 (memo linter) and O5 | With `narrate/` | candidate |

Not adopted:

- The strategies themselves (momentum/quality composite, v1 and v2) and their
  parameters: research decisions belong to this project's own replay on its
  own sample, never copied.
- Filing-category "orders" study and volume-anomaly variants as signals: only
  as a check on our `technical_signal` design after the trials log exists.
- DuckDB and parquet as the store: our stack is Postgres plus object storage.
- Momentum screen for real money: the repo itself calls it calibration only.
- Its email pipeline and news-to-data bridge from Marketaux/RSS feeds: news
  follows the extract-then-resolve rules already in CLAUDE.md.
- Scraping the NSE announcement and corporate-action endpoints as it does:
  `web_scrape` class, off in `commercial` mode; use archives and the drop
  folder first.

### From Automated-Fundamental-Analysis (faizancodes)

No licence file: ideas only. A 323-line script that scrapes Finviz for every US
stock, grades each metric against its sector's distribution, and a Streamlit
app for sector comparison. Small and simple; most of it is a poor fit, and its
data collection is the kind we forbid. Read: README, `stockgrader.py`,
`WebApp/app.py`, `WebApp/utils.py`.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| G1 | Sector-relative grading: each metric is placed against the distribution of its own sector (outliers trimmed by standard deviation twice, then the 10th or 90th percentile as the top grade and one fifth of the standard deviation as the step). Lower-is-better metrics (P/E, PEG, P/S, P/B, P/FCF, volatility) are flipped | Screens, `financial_facts` context, A13 | R1: pure function with the trimming rule and metric direction table hardcoded under `rule_version`. Shown as description, never a trigger, until replay on a later period | After MVP | candidate |
| G2 | Show where a stock sits inside its sector and industry distribution (histogram with the stock marked), with the sector switch between sector and industry | Reports, later frontend | R2: the peer set is the members known at `t`; peers come from the curated `sub_index`, never a model. Our universe is 100 names, so a sector may have only a handful: state the peer count on every chart and show nothing under a minimum | Later | candidate |
| G3 | Sector comparison view: median of a metric per sector; two sectors side by side for one metric; two metrics scatter with trendline and R-squared | Reports, replay analysis | R1: statistics in code. A correlation across 100 names is descriptive only | Later | candidate |
| G4 | Category grades (valuation, profitability, growth, performance) combined into one 0-100 rating | none | Rejected as a trigger: the weights (a fixed 6.2 multiplier) are arbitrary. A single number invites use as a buy score. Categories may be shown separately as context | rejected | rejected |

Not adopted, with the failures worth remembering as tests:

- Missing value becomes 0 (`get_metric_val` returns 0 on any error), so a
  stock with no data is graded as if it reported zero. Our rule: a missing
  value is never zero (O1); it yields N/A and the metric is dropped from the
  category with the drop shown.
- Unmatched values default to grade `C`: an unknown looks average. Same fix.
- Snapshot only: no `as_of`, no history; grades computed on today's sector
  distribution cannot be replayed. Sector distributions must be recomputed from
  data known at `t`.
- Proxy rotation and random user agents to scrape Finviz (a free proxy list
  scraped at start-up): the escalation the Breakage rule forbids, and it also
  routes traffic through unknown third-party proxies.
- Bare `except` returning defaults and printing a parse error: the F9 failure
  class.
- Price performance (1-month to year returns) as a grade input: momentum inside
  a fundamental score, and outcome data. Ours keeps technical signals a
  separate category.
- Analyst target price minus price as a "percent diff": media-reported targets
  are `brokerage_call` and never consensus.
- US-only data, streamlit app, Excel workflow.

### From Stock_Screeners_Raw (devfinwiz)

GPL-3.0: copyleft, so ideas only and no code copied. A desktop tool for NSE
tickers: pulls Yahoo Finance data per ticker into CSVs, then runs
book-value, P/S and EV/EBITDA screens, candlestick patterns, and a "fair
value" step. Small scripts, no tests. Read: README, `Scipts/` (all screeners,
`Valuation.py`, `DiscountValuations.py`, `PatternRecognition.py`,
`GarbageCollector.py`, `FinancialsExtractor.py`), the dataset layout.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| D1 | Screens as small named filters (price below book value by more than 10%, P/S under 1.25, EV/EBITDA between 0 and 11), each writing a dated output file | Screens, A13, `watchlist_entry` | R1: thresholds hardcoded under `rule_version`, tuned on one period only. Book value at or below zero and EBITDA at or below zero must be excluded explicitly (its EV/EBITDA filter does; its book-value filter had that check commented out) | After MVP | candidate |
| D2 | Prune-the-universe step that removes tickers with too little history and rewrites the ticker list | Universe | R2: pruning a list is survivorship bias. Ours records a delisted status from `corporate_action` and keeps the row | rejected | rejected |
| D3 | Pattern detectors (bullish and bearish engulfing, gravestone doji) run on the last two candles | `technical_signal` | Not in our signal list (volume spike, consolidation break, all-time high). A candlestick pattern has no tested edge; it would need replay and would still count as one technical category | rejected | rejected |

Not adopted:

- The "fair value" formulas (1.5 times book value; an average of sales per
  share and a P/S-adjusted price): invented constants with no derivation. Ours
  is reverse DCF and scenarios by code with stored assumptions.
- Every row is a snapshot with no `as_of`. Failures are printed and skipped
  (`except Exception: print`), and the loop then reuses a stale variable from
  the previous row (`hold2` in the P/S and EV/EBITDA filters). The F9 failure
  class again; worth a test that no filter reuses a previous row's variable.
- Yahoo Finance via `yfinance` and `yahoofinancials`: unofficial and rate
  limited; not an approved source class.
- Tkinter desktop front end and the mail script.
- The dataset is a checked-in snapshot of about 1,200 NSE tickers as CSV. It is
  useful only as a sanity check on our symbol list, never as input.

### From fooltrader (foolcage)

MIT: code may be copied with notice. Deprecated by its author in favour of zvt
(already reviewed as Z1–Z9). An older Chinese-market framework (Scrapy spiders,
CSV store, Elasticsearch and Kafka, a trader). Read: `docs/design.md`,
`docs/contract.md`, `fooltrader/api/technical.py` and `fundamental.py`,
`fooltrader/proxy/`, `sched/`, `tests/`. Most is superseded by zvt; only what
zvt does not show is recorded.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| H1 | Data contract document: security id, storage layout, every column, and event tables in one file, with the code held to it (the crawler adapts to the format, not the reverse) | `docs/`, ingest adapters | Matches the strict-Pydantic-per-adapter rule; keep one reference doc listing each store's columns and `as_of` | With ingest | candidate |
| H2 | Unified security id `type_exchange_code` | Entity table | Rejected: ours is ISIN, which survives ticker changes | rejected | rejected |
| H3 | Adjusted prices stored three ways (unadjusted, backward-adjusted, forward-adjusted) with the factor kept on each row; returns use the backward-adjusted one, screening the forward-adjusted one | Price adjustment factors, `technical_signal` | R2: its forward-adjusted price is computed from the latest factor, so old prices change when a new split arrives. That is a look-ahead. Ours: adjust with only the factors known at `t` | With `corporate_action` | candidate |
| H4 | Finance read functions take `report_period` and `report_event_date` as separate filters, plus a report-publication event table (results announcements, earnings forecasts) | `financial_facts`, `scheduled_event`, Z4 | R2: confirms the split of period from publication date. In India, results announcements are board-meeting outcomes in `scheduled_event` | With `scheduled_event` | merged into Z4 |
| H5 | Market-value-to-GDP and index PE-band views (share of time an index PE sat in each range) | `force`, `market_flow`, reports | R1: pure statistics from official series, display only. Needs dated NSE index PE data and GDP with real release dates, never revised history (R2) | Later | candidate |
| H6 | Trader design: strategy framework decoupled from the trading gateway, with event-driven and time-walk modes | Replay harness | The decoupling is the useful part; the trader itself is execution | rejected | rejected |

Not adopted:

- The order-capable trader, account service and buy/sell strategy base class:
  same reasons as the earlier trader rejections.
- Proxy manager, proxy spiders and "checked proxy" lists per domain: the
  escalation the Breakage rule forbids.
- Elasticsearch, Kafka and Kibana: new services before measurement.
- Crypto and futures data sources (ccxt, EOS, SHFE).
- Its tests are two thin files that call live endpoints; nothing to take.

### From FundamentalsQuantifier (JerBouma)

GPL-3.0: ideas only, no code copied. A Dash website that plots fundamentals
of about 6,000 companies from the FinancialModelingPrep API, filterable by
sector and industry. Read: README, `app.py`, `utilities.py`, the JSON data
files. Small; the same author's FinanceToolkit was already reviewed (K1–K22).

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| Q1 | Multi-company comparison view: choose a sector and industry, pick companies from the filtered list, and plot one metric for all of them, linear or log, annual or quarterly | Reports, later frontend, G2 | R2: the filter list comes from the curated `sub_index` and members known at `t`. Data comes from our stores, not a third-party API at view time | Later | candidate |
| Q2 | Metric menus generated from data files (a JSON list of items per statement) rather than hardcoded in the UI | Reports | Ours would be generated from the `financial_facts` schema so a new field appears without UI code | Later | candidate |
| Q3 | Growth of every statement item over set intervals, as a chart family | K1, `financial_facts` | R1: computed by code; the growth windows are a hardcoded table | With K1 | merged into K1 |
| Q4 | A list of companies with no data kept in a file (`no_data_companies.json`) so the UI never offers them | Universe, coverage gate | Fits O4: derive it from stored facts with `as_of <= t`, not a hand list | With O4 | merged into O4 |

Not adopted:

- Calling a paid vendor API live from the UI on every selection: our reports
  read stores through `pit.py`.
- Bank line items (deposit liabilities) sit in one flat balance-sheet menu
  with non-banks; ours keeps separate field sets (B8, Z5).
- Investor quotations as the README premise: not a design input.
- Dash and Heroku hosting.

### From FinanceDatabase (JerBouma)

MIT: code may be copied with notice; the data is community-edited and
sourced from third parties, so its licence for reuse is unconfirmed. A
300,000-row catalogue of equities, ETFs, funds, indices and currencies
(symbol, name, sector, industry, exchange, country, ISIN, FIGI, delisted
flag), stored as CSV per exchange, with a small query library. The Indian
part is thin (25 rows for NSE; 3,857 for BSE). Read: README, `financedatabase/`
(query classes, `validation/validate_identifiers.py`), `tests/`
(`test_invariants.py`, `test_validate_identifiers.py`), the CI workflows, the
compression notebook, `database/equities/NSI.csv`.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| N1 | Validate identifiers by checksum: ISIN check digit (via `python-stdnum`) and canonical format; flag any that fail | Entity table, ingest validation | R1: pure function. Our entity key is ISIN, so a mistyped ISIN in a hand-curated alias or sample table must fail at load | With entity table | candidate |
| N2 | Identifier repairs are deterministic and separate from validation: an audit reports issues; a cleanup applies only repairs a rule can prove (canonical casing, a stray `.0` suffix from a spreadsheet, a value corroborated by another valid identifier); everything else is left and reported | Quarantine, alias table review | R1, R3: repairs are never guessed. Ours writes a new row with provenance and never edits history | With entity table | candidate |
| N3 | Cross-field consistency: an ISIN's country prefix must agree with which other identifiers can exist (a US ISIN embeds its CUSIP; an Indian ISIN carries no CUSIP) | Entity table | R1. For India: `INE`/`INF` prefix, and the issuer code in characters 3–7 for linking renames (Z19) | With entity table | candidate |
| N4 | Read-rewrite is byte-identical test: loading and re-saving the source files must not change any file that was not intentionally edited | Curated tables, drop folder | A test that curated CSVs (alias, sub-index) round-trip without formatting changes | With curated tables | candidate |
| N5 | Cross-table invariant tests in one file (same columns, no duplicate keys, no empty keys) | `tests/` | Matches the schema-parity idea in Z27 | With migrations | candidate |
| N6 | Contribution model for curated tables: plain CSV that a non-programmer can edit, checked by CI on every change | `sub_index`, alias table | R1: hardcoded by design. The family edits through reviewed changes and tests validate them. The exposure matrix stays code, not CSV | With curated tables | candidate |
| N7 | Ship tables as bz2 CSV, chosen after measuring pickle, hdf and csv; pickle avoided because loading one can run code | Blob storage, drop folder | Never load pickle from an untrusted source. Adds a line to O10 | With ingest | merged into O10 |
| N8 | A `delisted` flag on each catalogue row | Entity table | R2: a bare flag has no date. Ours needs the delisting `corporate_action` and its ex-date | rejected | rejected |

Not adopted:

- Using it as a universe or sector source: it has no `as_of`, so today's
  sector would be applied to past dates (R2), the classification comes from
  Yahoo-style data, and NSE coverage is very thin. Sector and industry for us
  come from NSE's own classification with dates, or the hand-curated
  `sub_index`, never a model.
- Search over company summary text: the summaries are vendor text, not a
  source for peer sets (R1).
- ETF, fund, crypto, currency and money-market catalogues: out of scope.

### From ai-berkshire (xbtlin)

MIT: code may be copied with notice. A collection of 21 Claude Code / Codex
skills (markdown prompts, mostly in Chinese) plus 15 stdlib Python tools, and
thousands of generated reports. ai-value-investing-agents (A1–A18) is derived from this repo, so
AB5, AB1, AB6, AB10 and AB13 repeat A13, A14, A15, A17 and A18; the extra detail here
is what is new. Four investor "master" personas and a team
lead, aimed at US, HK and China A-share stocks. Its claim of audited live
returns is marketing, not evidence, and is not used. Read: README, `CLAUDE.md`,
`skills/` (`quality-screen`, `thesis-drift`, `investment-checklist`,
`income-investment`, headers of the rest), `tools/financial_rigor.py`,
`report_audit.py`, `terminal_value.py`, `reports_index.py`,
`momentum_backtest_v2.py`, `stock_screener.py`. The skills tell a model to do
the arithmetic and the judging, so their workflows are R1 violations as
written; what carries over is the checks, and the tools are the good part.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| AB1 | Report audit: pull every number out of a finished report, sample about 15% at random with a seed, compare each with a second source, and return pass or send-back. A report that fails is a draft | `narrate/`, reports, B9, Z16 | R1: ours is stronger, because every number already traces to a computation. Use it as a test that each rendered number appears in the stored data it cites, checking all numbers, not a sample. Its extractor bug is worth a test: `-1.72%` was read as `1.72` until the sign was captured (ASCII minus, U+2212, en-dash, full-width) | With `narrate/` | merged into B9 |
| AB2 | Cross-validate a field across sources; flag a deviation above 1% and stop above 5% | Ingest, Screener validation, Q-series checks | R1: pure function with a stored tolerance. Matches the Screener validation step and the two-source idea in Z2 | With XBRL parser | candidate |
| AB3 | Market-cap check: price × shares outstanding against the reported market cap, with likely causes listed when they differ (stale share count after a buyback or issue, unit or currency mismatch) | `financial_facts`, price adapter | R1, R2: share count taken as known at `t`. Catches a stale share count, the failure behind most bad per-share ratios | With XBRL parser | candidate |
| AB4 | Exact `Decimal` engine for valuation ratios (P/E, P/B, FCF yield, dividend yield) verified from raw inputs, and a Benford digit test on a series of reported values | `core/compute/` | R1. Matches the Decimal rule. Benford is a screen for hand-made numbers only, and small samples give noise; display-only flag, never a severity input | With ratios | candidate |
| AB5 | Quality screen of seven hard exclusions: 10-year average ROE below 8%; 5-year cumulative free cash flow negative; interest cover under 2; gross margin under 15%; operating cash flow to net profit under 0.7; net margin under 5%; share count up over 20% in 5 years (not from mergers). Banks and insurers skip the interest test. Three exemption rules stated in the spec | Screens, D1, G1, `watchlist_entry` | R1: thresholds hardcoded under `rule_version`, chosen on one period and tested on a later one. Its numbers are unvalidated starting points, not adopted values. The exemptions are hand-picked to spare known winners (its own examples are Meituan and Amazon), which is hindsight: ours must be fixed before the replay period. Cash-to-profit and dilution tests match our durability metrics | After MVP | candidate |
| AB6 | Thesis drift: split any change into fact change, price change and wording change, and only count the first. Wording differences between two reports are not drift | `thesis` / `thesis_condition`, R3 | Ours is already stricter: conditions are stored with a metric, operator, threshold and `observe_on`, and code evaluates them, so wording cannot drift. Adopt the framing as a rule in reports: a price move alone never changes a thesis status | Already designed | merged into thesis store |
| AB7 | Information-richness grade (A well covered, B partial, C thin) printed on each company, and a "gray zone" outcome when data is missing, distinct from both pass and fail | Coverage gate (O4), report labels | R1: compute the grade from stored coverage (years of filings, share of fields present, transcripts available), never a model's judgement. A missing field is `unknown`, not zero and not a fail | With O4 | merged into O4 |
| AB8 | Quick-kill list: a short list of red lines where any one is a veto before deeper work (fraud, unexplained auditor change, accounting restatement, promoter pledge) | `company_event`, screens | R1, R3: each line is a coded condition with an evidence URL, and a hit sends the name to review rather than deleting it | With `company_event` | merged into `company_event` severity |
| AB9 | Contrarian check: for each bullish claim, list who is short or negative and why | `thesis`, brokerage calls, Z14 | R3: the counter-case is stored data (bearish brokerage calls, promoter selling, rating downgrades from our stores), not model prose. Same as Z14 | With `brokerage_call` | merged into Z14 |
| AB10 | Terminal-multiple tool: exit P/E = (1 − g/ROIC) / (r − g), with three stated disciplines: r and g in the same currency and inflation basis; require r − g of at least 5 points or warn; treat discrete risks (delisting, regulator, supply cut) as a separate tail scenario, never as a higher r | Reverse DCF and scenario valuation | R1: pure functions, assumptions stored with their hash. The 5-point guard and the tail scenario are the useful parts, and the second rule matches our bear value being a separate stored scenario. Indian `r` and `g` are nominal INR | After MVP | candidate |
| AB11 | Ten-year IRR from profit, market cap, exit P/E, years and payout, plus a sweep over `r` with a check that the ranking of companies does not change | Scenario valuation, replay | R1. The ranking-stability check is the idea: a conclusion that flips with `r` is not a conclusion | After MVP | candidate |
| AB12 | Income names: durable distributable income versus a yield trap, judged from payout, cover by free cash flow, and net cash, with tax treatment left unknown unless the account type is given | Corporate-action dividend rules, holdings reports | R1: this repeats the CLAUDE.md rule on dividends not covered by free cash flow. Tax is context only | After MVP | merged into corporate-action rules |
| AB13 | Every claim in a report is labelled Verified fact, Estimate, Assumption or Analytical judgment, and every time-sensitive number carries its date | Reports, B9, `narrate/` | R1, R3: label chosen by code from the value's origin (parser, computation, stored assumption, model prose), not by the model | With `narrate/` | merged into B9 |
| AB14 | Index of all reports generated from file metadata (front matter first, then file name, then body, then commit time), written as `index.json` plus a readable README, with a check mode for CI; also a git-ignore check so private files never reach the index | Reports, docs | R2: use the report's own `as_of`. Reports stay private to the family, so the ignore check matters | Later | candidate |
| AB15 | Kelly-style odds and position framework across many companies (upside, downside, probability, size) | Sizing baseline | R1, R3: sizing is a rule with user-set parameters. Its probabilities are model guesses, so they stay out; a Kelly figure from invented odds is false precision | rejected | rejected |
| AB16 | News-pulse: attribute a price move to an event within the hour and give a hold or act verdict | News timeline | R2: the reaction is outcome data and may be shown but never fed back to a prompt; a same-day verdict is the trading behaviour we avoid | rejected | rejected |
| AB17 | Supply-chain bottleneck hunter: find the scarce input in an industry and the listed companies that own it | `relationship_edge`, `sub_index` | R1: a model finding the peers or constituents violates the sub-index rule. Only a human-curated bottleneck list could enter, as a `sub_index` | rejected | rejected |
| AB18 | Codex skills generated from the Claude skills by a sync script, so two copies never drift | Docs | If this project ever ships agent skills, generate the second copy from one source | Later | candidate |

Not adopted:

- Four investor personas, a team lead and star scores (the 11-file "master"
  skills): model verdicts with no data test, and already parked as A11/A12. A star score from a model breaks R1 and R3.
- Forced verdicts with tiered "buy at price X" recommendations: our output is
  an assumed baseline with the review names and disclaimer, and sharing
  buy-style calls outside the family is the SEBI risk in CLAUDE.md.
- Momentum backtests on three hand-typed stocks (NVDA, AMD, MU), with quarter
  figures typed in from memory and picked after the fact: hindsight and
  survivorship (R2), outside the replay harness. Its screener then turns the
  result into position sizes of 3%, 5% and 8%; parameters tuned on the same
  history they are shown to work on.
- Hand-entered fundamentals as ground truth ("more accurate than the API"):
  our facts come from XBRL parsers only.
- `xueqiu_scraper.py`, `twstock_data.py`, `ashare_data.py`: China and Taiwan
  scrapers of the `web_scrape` class.
- Advice to run with `--dangerously-skip-permissions`: never for this project.
- Skills that tell the model to fetch data and do arithmetic itself
  (`financial-data`, `earnings-review`): R1. The tools it also ships are what
  its own text says to use, which shows the skills are a weaker version of the
  tools.
- Public marketing content (return charts, WeChat article skill, star history).

### From claude-equity-research (quant-sentiment-ai)

MIT: code may be copied with notice. One Claude Code slash command (a single
prompt file, `/trading-ideas:research TICKER`) that runs web searches and
writes a Wall-Street-style report with a BUY/SELL/HOLD rating, a price target,
scenario probabilities and a position size, plus a risk-profile config and three
docs. There is no code, data store or test. Read: README, `research.md` (the
command), `docs/methodology.md`, `docs/customization.md`,
`config/config.example.json`, both sample reports. Very little transfers: it is
the opposite of this project's design, since a model writes every number.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| CE1 | Risk profiles as parameter sets (conservative, moderate, aggressive) each holding a maximum position size, a stop-loss percentage and a beta limit | Sizing baseline, concentration cap, drawdown review | R1: the user sets these, and code applies them. Its values (2/4/7% position, 5/8/12% stop) are examples, not adopted. Its `min_conviction_score` is a model number: dropped | After MVP | merged into sizing parameters |
| CE2 | Per-sector list of the metrics that matter (net interest margin and credit quality for banks; reserve life for energy; pipeline for pharma) and sector-native multiples (P/AUM, P/deposits) | `financial_facts`, B8, G1 | R1: a hardcoded per-sector metric table under `rule_version`, computed from XBRL. Repeats B8 and Z5 | With B8 | merged into B8 |
| CE3 | Catalyst section split into near-term (0–6 months), medium-term and event-driven (M&A, index inclusion, spin-offs, special dividends), each with a date | Report, `scheduled_event`, `index_event`, `corporate_action` | R1: filled from those stores only, dated and sorted, never from a model's search. Strategic "initiatives" prose is left out | With `scheduled_event` | merged into `scheduled_event` |
| CE4 | Every metric carries its period (YoY, QoQ) and every price target names the firm that issued it | Reports, `brokerage_call`, B9 | R1: same as AB13 (label the origin of each number). Broker name and date come from the stored `brokerage_call` row | With `narrate/` | merged into AB13 |

Not adopted:

- BUY/SELL/HOLD, a price target with upside, a conviction level and a position
  size in one summary table: this is the call-style output CLAUDE.md forbids
  outside the family, and the SEBI research-analyst risk. Ours shows the review
  names, assumed baselines and the disclaimer.
- Bull/base/bear probabilities (25–30%, 50–60%, 15–25%) and a probability-weighted
  target: invented numbers, and a single expected value hides the range. Ours
  stores bear, base and bull with assumptions; any probability would be a stored
  user assumption.
- Sample report `AAPL_analysis.md` is a warning, not a model. Dated
  10 September 2024, it cites "Q4 2024" results that were not reported until
  31 October, gives peer revenue for FY2022 next to current quarters, and lists
  no source. That is look-ahead and stale data written as fact (R2). A test that
  no figure in a rendered report has an `as_of` after the report date covers
  it; add it to AB1.
- Options flow, put/call ratios and implied volatility, ESG scores: out of
  scope for the universe and unsourced (searched from the web).
- Real-time web search as the data source: R1, R2 and the source-class rules.
- Plugin packaging, star-history workflow and marketing badges.

### From StockAnalysisSystem (chm020924)

No licence file: ideas only, no code copied. A small China A-share screener in
about 1,700 lines of pandas: it reads 50 CSVs of daily rows (price, market cap,
PE, PB, MACD, RSI), filters, scores, "predicts" 5-day and 15-day returns, and
writes an HTML page with per-stock candlestick charts. No tests, no dates on
results. Read: README, `indicators.py`, `models.py`, `analyzer.py`,
`data_loader.py`, `main.py`, the CSV layout, the example report.

| # | Idea | Where it fits | Rule check | When | Status |
|---|---|---|---|---|---|
| SA1 | Each filter returns the reason a stock was excluded ("market cap too small", "PE abnormal") and the report can list the excluded with their reasons | Screens, D1, `watchlist_entry` | R1: reason is a code from the rule that fired, stored with `rule_version`. Matches "a blocked signal is still reported, with the gate that blocked it" | After MVP | merged into D1 |
| SA2 | Minimum-history guard: a name with fewer than 60 rows is skipped before any factor is computed | `technical_signal` | R2: return "insufficient history" with the count, never a value. Its 120-row cut is also a reminder that a window must be counted in sessions known at `t` | With `technical_signal` | candidate |
| SA3 | Per-stock report page: sortable table with the score broken into named parts (valuation, growth, liquidity, technical, position, stability) and a click-through candlestick chart with MACD/RSI panes | Reports, later frontend, Q1 | R1: show the parts of a coded rule (for example each `ADD_REVIEW` gate and whether it passed), not an LLM-weighted score. Chart data read through `pit.py` with prices adjusted only by factors known at `t` | Later | candidate |
| SA4 | Surveillance and small-cap "operator" exclusions (its ST list, amplitude and turnover caps to drop manipulated stocks) | Universe, `company_event` | India analogue is NSE's ASM/GSM surveillance lists and trade-to-trade segment. Source class to confirm when built. Rarely relevant for Nifty 100, so low priority | Later | candidate |
| SA5 | Data sanity on the input file: it carries negative prices (-2.49 in 1991 rows, from backward adjustment) and blank valuation columns | Price adjustment, Z20 | R1, R2: adjusted prices must stay positive and the raw price is kept beside the factor. Add a test for non-positive prices | With `corporate_action` | merged into Z20 |

Not adopted:

- The 5-day and 15-day return "prediction", a weighted sum of momentum, a
  technical score and a fundamental score, divided by constants: an invented
  formula with no test, and a "confidence" figure built from the same inputs.
  A forecast with no replay breaks R1 and R3.
- The "fundamental score" is mostly price: its growth points come from 60-day
  price return and its liquidity points from turnover; only PE and PB are
  fundamental. Price momentum standing in for growth is the confusion the
  facts store exists to avoid.
- Whole-history weights (60% fundamental, 40% technical) and point bands (PE 8–15
  scores 15): tuned by eye on no held-back period.
- Every computation reads `iloc[-1]`, the last row of the file, with no `as_of`,
  so a rerun on new data changes history (R2).
- Broad `except: print` and continue, so a bad file silently drops a stock (the
  F9 failure class).
- Alpha "factor" library (5/20/60-day momentum, volatility, volume ratio): the
  raw ingredients are ours already (volume spike, days of volume); more tunable
  parameters on full history is the overfitting the project forbids.
- China-market filters and data (ST, 50-billion-yuan cap, A-share CSV export).

### Rejected from TradingAgents (recorded so it is not re-proposed)

- Trader and portfolio-manager agents that approve or execute orders: execution is out of scope.
- LLM analysts computing indicators or judging fundamentals: R1.
- StockTwits and Reddit sentiment agents: social sources sit next to the X exclusion; sentiment already triggers nothing until replay validates it.
- LangGraph orchestration: no agent orchestration until the stores have data.
- Yahoo Finance as a source: not in the source table.
- Polymarket data: out of scope.

## Open decisions (from the same discussion)

1. **Renaming the actions.** Decided 2026-09-29: keep `ADD_REVIEW`, `TRIM_REVIEW` and `EXIT_REVIEW` as they are. Revisit only if they prove confusing. Plain-English meanings for newcomers are below.

   | Action | Plain meaning | Raised when | What you do |
   |---|---|---|---|
   | `ADD_REVIEW` | "Look at whether to add more." A prompt to review, not a buy call. | All gates pass: signals from two independent categories, price below base or bull value, thesis intact, weight within your cap. | Read the evidence and decide. A blocked signal is still shown, with the gate that blocked it. |
   | `TRIM_REVIEW` | "Look at whether to reduce." | Price above bull value; a holding above its maximum weight; pro-forma dilution beyond the threshold with no offsetting return. | Check whether the position has become too large or too expensive. |
   | `EXIT_REVIEW` | "Look at whether the reason for owning it has failed." The most serious of the three. | A thesis condition fails; or a critical alert (pledge invoked, large promoter sale, pledged percentage jump). Drawdown alone raises a review, not this. | Re-read the thesis and the facts before any decision. |

   No action is a sell or buy order. Holdings are never removed automatically, and every display carries the "Assumed baseline" disclaimer.
2. **Hold indication.** There is no `HOLD` action; holding is "no review flag raised". Proposal: a display-only status, "No review triggered as of {date}", listing the checks that passed, with the standard disclaimer, never stored as an action. **Awaiting a decision.**

## Planning gate

When the repository list above is fully reviewed:

1. Merge duplicate ideas across repos.
2. Drop anything that breaks R1–R3 or the "Things to push back on" list.
3. Order by dependency (what needs data in the stores first).
4. Turn the survivors into session plans, in the same style as the existing session plans.
