# Corporate actions reference

**Last updated:** 2026-09-29

Background notes for store ⑮ (`corporate_action`) and the price adjustment
factors computed from it. CLAUDE.md holds the rules; this file holds the
domain detail behind them — what each action type does to a share and to a
price. The code is the specification for how it is done: `core/db/models.py`
(the row), `ingest/corporate_actions/` (the parsers),
`core/compute/adjustment.py` (the arithmetic) and `core/db/pit.py` (the
reads).

## Lifecycle

```
ANNOUNCED → APPROVED → DATES_SET → EFFECTIVE → COMPLETED
          ↘ REVISED (any stage)
          ↘ WITHDRAWN (any stage)
```

Each stage is a new append-only row for the same `action_key`, with its own
`as_of`. A read takes the newest version known at `t`; nothing is updated in
place. Only a *final* version adjusts prices — an action still `ANNOUNCED` or
`APPROVED`, or one `WITHDRAWN`, adjusts nothing, and the reason is returned
alongside the result rather than dropped.

Event time is the ex-date. An action with no ex-date yet cannot adjust a price,
however certain its terms look.

## Action types and what they do to a price

Only capital actions change the share count or the face value behind a price.
Dividends do not: they leave the share alone, and a total-return series is a
separate thing from the price series.

| Type | Terms carried on the row | Price effect |
|---|---|---|
| split | `face_value_from`, `face_value_to` | sub-division: ₹10 → ₹2 scales past prices by 0.2 |
| consolidation | `face_value_from`, `face_value_to` | reverse: ₹1 → ₹10 scales past prices by 10 |
| bonus | `shares_new`, `shares_held` | 1:1 scales past prices by 0.5 |
| rights | `shares_new`, `shares_held`, `issue_price` | theoretical ex-rights price ÷ last cum-rights close |
| demerger | `retained_fraction`, `new_isin` | the parent's share of pre-demerger value, from the company's own cost apportionment |
| dividend | `dividend_per_share` | none on the price series |
| isin_change | `new_isin` | none; drives the entity lineage |

Those seven are what `CorporateActionType` carries today. The rest of the list
in CLAUDE.md store ⑮ — buybacks, fund-raising (QIP, preferential allotment,
warrants, ESOPs, FCCBs), mergers, capital reduction, name and symbol changes,
delisting, suspension — is not in the enum yet. They are tracked for dilution
and event signals rather than for price adjustment, and each needs its own
terms on the row before it can be stored; adding one is a migration plus a
parser change, not a new enum member alone.

Face values, not a printed ratio, drive splits: the exchange publishes the face
value change, and it is unambiguous where a "1:2" is not.

### Why rights needs a market price

A rights issue is priced below market, so the entitlement has value and the
past price must be scaled by the theoretical ex-rights price over the last
cum-rights close. That close is looked up from the exchange bhavcopy (Upstox
second) within a bounded window before the ex-date. If no close is found, the
action is skipped with that reason — it is never approximated.

## Where the terms may come from

`ratio_basis` records how the terms were obtained, and it gates adjustment:

- a structured exchange field adjusts prices directly;
- terms read from a PDF adjust nothing until a named person has verified them
  (`verified_by`).

This is the R1 boundary in practice. A model may quote the text of an
announcement; it never supplies the number that moves a price.

## Reading adjusted prices

Adjusted closes are read through `core/db/pit.py`, never by multiplying prices
by hand:

- `price_adjustments_as_of` — the factors known and effective at `as_of`, plus
  every action left out with the reason why;
- `adjusted_closes_as_of` — bhavcopy closes across the ISIN lineage with those
  factors applied;
- `isin_lineage_as_of` — the ISINs a company has used, so a history that spans
  an ISIN change is continuous.

Only factors whose ex-date is *after* a given date apply to that date's price;
`cumulative_factor` does that product. Volume and open interest are counts and
are never adjusted.

Upstox candles are deliberately not used for adjusted series: whether the
vendor has already applied an adjustment is checked against the bhavcopy by
`upstox_bhavcopy_crosscheck_as_of`, not assumed.

## Ingestion

Two drop-folder parsers read corporate actions today (see
`ingest/corporate_actions/adapters.py` for their names, and the fixtures under
`tests/fixtures/corporate_actions/` for the file formats): the exchange's own
corporate-action file, and a curated file for actions whose terms had to be
read by a person. Both save raw bytes before parsing, and a row that cannot be
parsed into unambiguous terms goes to quarantine with a reason rather than
being guessed at.

## Related

- CLAUDE.md → "Data model", store ⑮, and the ownership/index/corporate-action
  signals under Decision support
- docs/temporal-model.md → `as_of`, event time and lifecycle semantics
