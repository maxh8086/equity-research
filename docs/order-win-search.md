# Finding order wins in exchange filings

Store ⑦ (`order_win` / `order_status`) needs candidate filings and a decision
about each one. This records where the candidates come from, why the obvious
search does not work, and what is settled versus still open.

Not built yet: no adapter, no store, no migration. `core/compute/order_terms.py`
is the decision half, which is source-agnostic and testable on its own.

## The problem: "order" mostly means a tax order

In NSE/BSE Regulation 30 filings the word *order* is dominated by GST demands,
assessment orders and tribunal rulings, filed under the same regulation as a
genuine contract award. A naive `"order received"` search returns mostly tax.

Two traps sit on either side of that:

1. **Boilerplate.** Nearly every announcement cites *SEBI (Listing Obligations
   and Disclosure Requirements) Regulations, 2015*. Excluding on `sebi` or
   `regulation 30` excludes the entire corpus. `classify` redacts that
   boilerplate before matching exclusions, and `search_query` never negates it.
2. **Precedence.** With no parentheses, a search engine binds `or` more loosely
   than the implicit `and`, so `-"tax" "a" or "b"` filters only `"a"` and every
   other branch comes back unfiltered. The disjunction must be parenthesised
   with the negatives outside it.

## Two stages

- **`search_query()`** cuts the candidate set down. It negates tax and legal
  wording, so most tax orders never arrive.
- **`classify()`** makes the careful call on what did arrive: `AWARD`,
  `PRE_AWARD`, `EXCLUDED`, `AMBIGUOUS` or `NO_MATCH`, plus every INR figure
  parsed by code with its verbatim quote.

Both read the same hardcoded phrase lists, so recall and precision cannot
drift apart. The lists are research decisions and belong in code, under
`rule_version` (CLAUDE.md, Code conventions).

`AMBIGUOUS` means quarantine. Letting the exclusion win instead would be
quieter and wrong: a real award would vanish because a tax order shared the
page, and a gap must be visible rather than silently favourable (R2). The
volume worry that would motivate it is already handled by the query stage.

## Sources

| Source | Class | Standing |
|---|---|---|
| NSE / BSE announcements | `web_scrape` | The evidence. CLAUDE.md already classes announcements this way. Not yet built. |
| Screener full-text search | `web_scrape` | **Discovery only, and unconfirmed.** See below. |
| Drop folder | `manual_drop` | The fallback the Breakage rules require, and the way to start without any scraping at all. |

A reported order win is a lead, counted only once confirmed against an
exchange filing (CLAUDE.md, News). So Screener can suggest candidates; it can
never be the evidence a row is stored against.

### Screener: unresolved

`docs/mcp-repo-review.md` records that Screener's robots.txt disallows
`/api/company/search/`, `/screen/raw/` and `/api/company/{id}/chart/`. Whether
it also disallows **`/full-text-search/`** has not been checked — the session
that wrote this could not reach the host.

**Before any adapter is built against it, confirm:**

1. `/full-text-search/` is allowed in `https://www.screener.in/robots.txt`.
2. The boolean syntax. The query builder emits the common
   `-"term" ("phrase" or "phrase")` form; engines differ on precedence and on
   whether `or` must be uppercase. Paste one query by hand and check the
   result count against a term you know the answer for.

If robots.txt disallows it, that is the end of it: fall back to NSE/BSE
announcements, or to the drop folder. No impersonation, no headless browser —
CLAUDE.md, Breakage.

## Known false positives

Real, and left visible rather than papered over. All three land in
`AMBIGUOUS`, so they reach a person and never a store:

- **`letter of intent`** is also M&A wording. Caught by the acquisition terms
  in `NON_ORDER_CONTEXT_PHRASES`, but only when the filing names them.
- **`purchase order`** appears in related-party disclosures. Caught by
  `related party` the same way.
- **`work order`** appears in scheme-of-arrangement novations.

Reviewing quarantine is what grows these lists — the same loop as the news
alias table.

## Not covered

- Order *value* is parsed, but currency other than INR is not. A filing
  quoting USD is not recognised as an amount at all.
- Multi-order filings are not split: every phrase and amount is reported, and
  pairing an amount to an order is left to whoever builds the store.
- `order_status` (executed versus declared) needs the later execution updates,
  which are a different filing and not addressed here.
