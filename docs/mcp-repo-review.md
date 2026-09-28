# Review: screener-mcp and finstack-mcp, and what they should change

Reviewed 2026-09-28. Both repos read in full (finstack-mcp: 64 Python
modules, `src/finstack/{data,tools,utils}`, `dashboard-api/`, `landing-page/`).

## Verdict in one line

**Do not redesign the plan or the code structure around either repo.** Take
endpoints and format facts from both, take one concrete piece of code
behaviour from screener-mcp (request pacing), and reject the rest. The
existing structure — `ingest/` adapters, `core/compute/` pure functions,
`core/db/pit.py` reads, `extract/` + `narrate/` for the model — is not what is
holding this project back; empty stores are.

---

## 1. finstack-mcp — measured against the non-negotiables

MIT (c) 2026 SpawnAgent. Counts across all 64 `.py` files under `src/` and
`dashboard-api/`:

| Probe | Count | Meaning |
|---|---|---|
| `as_of`, `asof`, `point_in_time` | **0** | No temporal model at all |
| `content_hash` | **0** | No provenance, no raw-bytes retention |
| `Decimal` | **0** | vs `float(` 194 — money is float throughout |
| `Mozilla` / `User-Agent` | 11 / 15 | Browser impersonation, incl. against NSE |
| `robots` | **0** | robots.txt never consulted |

`data/nse.py` sends `Chrome/120.0.0.0` to `nseindia.com`; other modules add a
`Referer: https://www.nseindia.com/`. That is precisely the escalation the
**Breakage** section of CLAUDE.md forbids.

Storage is `utils/cache.py` — an in-memory TTL cache. Nothing is durable, so
there is no append-only store, no replay, and no `as_of` to filter on. That is
not an oversight in their design; it is the design. finstack-mcp answers *what
is true right now*. This project answers *what was knowable at t*. Those two
requirements produce opposite architectures, and R2 is the rule that "cannot be
repaired later".

Other direct collisions:

- **`data/agents.py`, `research.py`, `sentiment.py`** emit `"BUY"` (38),
  `"SELL"` (36), `"HOLD"` (21) verdicts. Our actions are `ADD_REVIEW`,
  `TRIM_REVIEW`, `EXIT_REVIEW` — deliberately not buy/sell, partly because
  buy/sell calls leaving the family touch SEBI research-analyst rules.
- **`data/telegram_tracker.py`** tracks Telegram channels; `api.stocktwits.com`
  and `reddit.com/prefs/apps` appear too. Same category as the X exclusion.
- **`data/pump_detector.py`, `signal_tracker.py`** are short-horizon momentum.
  Out of scope for >12-month buy-and-hold.
- **yfinance in 27 of 47 data/tool modules** — Yahoo's unofficial endpoint, not
  an approved source class, and it would displace Upstox as the price source.
- **`payments.py`** (Stripe + Razorpay), `landing-page/`, `railway.json`: this
  is a commercial SaaS. Commercial use here needs exchange data licences.

**What is genuinely fine:** the broker adapters. Angel One, Upstox, Dhan,
Fyers and ICICI expose only `get_live_quote_*`, `get_market_depth_*`,
`get_candle_data_*`, `broker_status*`. I grepped for order placement across
`src/` and found none — the only hits are a `transactionType` field read out of
an NSE insider-trading payload and a docstring about bid-side depth. That
matches our "read-only aids, no order-capable tool" rule. Angel One does store
a TOTP secret (`pyotp`), which we would not do.

### The one thing worth taking: a source inventory

This is the real value. finstack-mcp names concrete endpoints for rows our
data-sources table currently marks *"confirm when built"*. Facts, not code:

| Our store / gap | Endpoint finstack uses |
|---|---|
| `shareholding_pattern` (live) | `nseindia.com/api/corporate-share-holding-category` |
| `insider_trade` | `/api/corporates-insider-trading`, `/api/corporates-pit?symbol=` |
| `bulk_block_deal` | `/api/snapshot-capital-market-largedeal` |
| `rating_action` | `/api/corporates-credit-ratings`; `api.bseindia.com/BseIndiaAPI/api/CreditRating/w` |
| `market_flow` | `/api/fiidiiTradeReact`; `/reports/fii-dii` |
| `scheduled_event` | `/api/corporate-announcements?index=equities&symbol=` |
| `mf_holding` | `portal.amfiindia.com/DownloadData.aspx`; `amfiindia.com/modules/AumReport`; `api.mfapi.in/mf` |
| `company_event` | `sebi.gov.in/sebi_data/rss/SEBIOrders.xml` (an RSS feed — cheap) |
| `force` intensities | `api.worldbank.org/v2/country/IN/indicator/{FR.INR.LEND,FR.INR.RINR}`; RBI `BS_NSDPDisplay.aspx?param=4` |
| annual reports | `api.bseindia.com/BseIndiaAPI/api/AnnualReport/w` |

Two notes on these. The `nseindia.com/api/*` paths are the internal endpoints
CLAUDE.md already classes as `web_scrape`, not `official_archive` — they carry
the archive-vs-scrape distinction we already made, and they are exactly what
NSE's anti-bot defences sit in front of. The SEBI RSS feed and the World Bank
API are the two genuinely new, genuinely clean finds: both are published for
programmatic use.

### Recommended: no plan change from finstack-mcp

The one defensible sequencing change it suggests is small and is not really
*from* finstack: `sebi.gov.in/sebi_data/rss/SEBIOrders.xml` is a published RSS
feed feeding `company_event`, and it is cheaper to build than the NSE
announcement scrape. Worth noting in `getting-started-plan.md` as a candidate;
not worth reordering anything now, because it is post-MVP either way.

---

## 2. screener-mcp — the adapter you asked for

MIT (c) Logesh Ramasamy. Small, single-purpose, and much closer to our shape.

**Add to the Reference repositories table:**

```
| LogeshR15/screener-mcp | MIT | Screener company-page structure; request pacing |
```

**Adopt into `ingest/http.py`** (this is the concrete code lesson): its shared
429 cooldown and pacing — `_MAX_CONCURRENCY = 3`, `_MIN_INTERVAL = 0.25s`,
retry on 429/503 honouring `Retry-After`, and a *shared* cooldown so one 429
pauses every in-flight request to that host rather than each task backing off
alone. Our `ingest/http.py` throttles per call; it has no shared cooldown. That
is a real improvement and it makes us a better-behaved client, which is the
direction CLAUDE.md wants.

**Reject three things from it:**

1. Its `Chrome/120` User-Agent — impersonation. We identify ourselves.
2. Its login/credential flow — we do not authenticate to Screener.
3. Three endpoints that Screener's robots.txt disallows:
   `/api/company/search/?q=`, `/screen/raw/?sort=&page=`,
   `/api/company/{id}/chart/?q=`.

That leaves `/company/<SYMBOL>/consolidated/` — robots-allowed and
unauthenticated — which is what the adapter should use.

### Adapter design (`ingest/screener_page/`)

Sits beside the existing `ingest/screener_export/`, same house shape
(`__init__.py`, `adapters.py`, `parser.py`), declared through
`ingest/base.Adapter` so `ingest/registry.py` discovers it:

```
name           = "screener_page"
source_class   = SourceClass.WEB_SCRAPE
target_stores  = ("screener_page_snapshot",)
```

- `EQUITY_SOURCE_SCREENER_PAGE_ENABLED` switches it;
  `EQUITY_WEB_SCRAPING_ENABLED=false` overrides it; `commercial` mode refuses
  startup with it enabled. All of that is already enforced by
  `registry.check_startup` — the adapter gets it for free.
- Raw HTML to blob storage keyed by `content_hash` **before** parsing.
- `as_of` is the fetch time (a company page carries no publication timestamp),
  recorded as such and never treated as the filing date.
- Strict Pydantic model, so a Screener layout change fails loudly. Contract
  tests on a recorded response plus a daily canary.
- Money as `Decimal`.
- **Its store is a validation sidecar, not a fact source.** Nothing in
  `financial_facts` may be sourced from it, and the `core/db/pit.py` reads for
  facts must not reach it. The point of this store is to compare our XBRL parse
  against Screener's numbers on the 20-company sample — which is what
  `validate/` already does with the Excel export, only without a human in
  Excel.

  Worth being plain about: this is the same job the Screener **export** path
  already does, and the export is the authorised, robots-clean route. The page
  adapter's only advantage is that it needs no human. If the fixed template
  makes the export loop painless, this adapter earns its keep only as a
  cross-check. You asked for it after I flagged that, so it is specified here —
  but build it *after* the export round-trip is green, not before.
- Migration **`0011`**, created on the `next-session-9036bc` branch
  (`next-session-938ec0` is already at `0010_screener_export`).

---

## 3. Structure: what I would actually change

Nothing structural. Three small additions:

1. `ingest/http.py` — shared per-host 429 cooldown (from screener-mcp).
2. CLAUDE.md Reference repositories — add `LogeshR15/screener-mcp` (MIT).
   I would **not** add finstack-mcp as a reference repo: taking endpoint URLs
   from it is fair use of public facts, but listing it invites someone later to
   copy code from a codebase with zero `as_of` and float money.
3. `docs/` — park the endpoint table above as a candidate-sources note, so the
   "confirm when built" rows in the data-sources table have a starting point.

The finstack-mcp review answers the question you asked — does this justify
redesigning the plan? — with a clear no, and the evidence is the five zeros in
the table at the top. Its breadth is tempting precisely because it skipped
everything that makes breadth expensive.
