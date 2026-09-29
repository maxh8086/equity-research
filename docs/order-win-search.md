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
| Screener full-text search | `web_scrape` | **Ruled out**, on two independent grounds. See below. |
| Drop folder | `manual_drop` | The fallback the Breakage rules require, and the way to start without any scraping at all. |

A reported order win is a lead, counted only once confirmed against an
exchange filing (CLAUDE.md, News). So Screener can suggest candidates; it can
never be the evidence a row is stored against.

### Screener: ruled out (checked 2026-09-29)

The open question was whether `/full-text-search/` is allowed. It was checked
against the live host, and the route is unusable for us on two independent
grounds. Either one alone would end it.

**1. It is behind an account wall.**

```
GET https://www.screener.in/full-text-search/
-> 302 Found
   Location: /register/?next=/full-text-search/
```

Unauthenticated requests never reach the search at all. Using it would mean
automating a logged-in session, which CLAUDE.md rules out in the same terms it
rules out X: an escalation past an access control, not a format change to
rewrap.

**2. Its query URLs are disallowed by robots.txt.** The live file is:

```
User-agent: *

Disallow: /user/*
Disallow: /*?q=
Disallow: /*?sort=
Disallow: /*?limit=
Disallow: /*?page=
Disallow: /company/source/quarter/*
```

Screener disallows by **query-string pattern**, not by path. `/full-text-search/`
is not named — and it did not need to be, because a full-text search without a
query is not a search, and the query rides in `?q=`, which `Disallow: /*?q=`
covers. The same rule is what actually disallows the three endpoints
`docs/mcp-repo-review.md` lists; that document is precise about it, citing them
as `/api/company/search/?q=`, `/screen/raw/?sort=&page=` and
`/api/company/{id}/chart/?q=`. An earlier summary here dropped the query
strings and so made the rule look path-based, which would have left
`/full-text-search/` looking clear. It is not.

The second point was not confirmed by issuing a disallowed request — that would
have been the violation itself. The login wall is what was observed directly;
the `?q=` reading follows from the robots file and the nature of the route.

**Consequence.** No Screener adapter for order-win discovery, and the boolean
syntax of `search_query()` is now unverifiable against this engine. Candidates
come from NSE/BSE announcements, or from the drop folder, which is where this
can start with no scraping at all.

`search_query()` itself stays. It is source-agnostic, it is the same phrase
lists the classifier uses, and whatever full-text engine is eventually pointed
at the announcement corpus will need a query. Its docstring already says to
confirm precedence and `or` casing against the target engine before relying on
it, and that caveat is now the live one: the syntax is **unconfirmed against
any engine**, not merely unconfirmed against Screener.

Nothing above depends on `/company/<SYMBOL>/consolidated/`, the robots-allowed
unauthenticated page `docs/mcp-repo-review.md` recommends for a different job.
That route is unaffected; it simply cannot do discovery, only lookup by a
symbol already known.

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
