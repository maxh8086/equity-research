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
> Build the Upstox v3 historical ingester. Instrument keyed by ISIN, not ticker. Daily candles from Jan 2000. Store raw prices plus corporate-action adjustment factors separately.

**Then run the adjustment test yourself.** Pick a company with a known split, pull the series across that date, look for a discontinuity. No gap means adjusted; a cliff means raw. Do it for a bonus issue too.

**Week 1 exit criteria:** architecture tests pass, 20 companies of price history loaded, entity table keyed by ISIN.

---

## Week 2 — Financial facts

Session 4 — XBRL parser
> Build the BSE/NSE XBRL parser for `financial_facts`. Deterministic element mapping, no LLM. Unmapped elements go to a quarantine table for manual review, never guessed.

Session 5 — ratios
> Implement ratio computation in `core/compute/ratios.py`. Pure functions, Decimal throughout, property tests. ROCE, margins, debt ratios, incremental ROCE.

Session 6 — validation
> Build a validation script comparing our computed ratios against Screener for 20 companies. Report divergences with the underlying line items.

**Week 2 exit criteria — the real gate.** Your numbers match Screener for 20 companies. If they don't, stop and fix. Everything downstream inherits these errors.

---

## Week 3 — First real signal

Session 7 — capacity detector
> Build the CWIP to gross block step-function detector. Pure arithmetic. Flag when gross block rises ≥20% QoQ while CWIP falls. Output to `company_event` with severity computed by code.

This is your first genuine output — capacity commissioning leads revenue by 2–4 quarters, and nobody sells it.

Session 8 — credit ratings
> Build the rating action parser for CRISIL, ICRA, CARE and India Ratings. Store ⑧ schema. Treat `withdrawn` as severity 2.

---

## Week 4 — The MVP

Session 9 — guidance extraction
> Build concall transcript extraction into `guidance_claim`. Pydantic schema with hedge_strength (will > expect > aim to > working towards), specificity, verbatim quote, source URL. Use Sonnet — do not downgrade this model tier.

Session 10 — resolution
> Build auto-resolution matching guidance claims against `financial_facts` when periods close. Implement SILENT detection for claims that stop being mentioned across two consecutive filings.

Session 11 — output
> Build a CLI report: per company, what management promised, what landed, what went silent, and the delivery rate weighted by hedge strength.

**MVP exit criteria:** run it on your five largest holdings, four quarters back. If the delivery rates don't surprise you, stop building and reconsider. If they do, you have something no Indian vendor sells.

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
2. Force timeline ③ and the hardcoded exposure matrix
3. Order book ledger ⑦
4. Reverse DCF and tri-scenario valuation
5. Relationship graph ④, events ⑤
6. Then, and only then, the swarm

---

## The one thing to get right this week

`as_of` on every table, filtered on every read, never backfilled.

Retrofitting it onto populated tables costs weeks. Getting it right on an empty schema costs an afternoon. It's also what makes replay possible — being able to ask "what did the system know in March 2027" without hindsight is the property that makes everything else trustworthy.
