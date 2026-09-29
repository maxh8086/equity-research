# Upstox API v3 Reference

**Last updated:** 2026-09-29

## Official Documentation Links

Keep these links up-to-date when Upstox changes their API:

- **API Overview:** https://upstox.com/developer/api-documentation/api-overview
- **Historical Data (main):** https://upstox.com/developer/api-documentation/historical-data
- **Historical Candle Data V3:** https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
- **Authentication:** https://upstox.com/developer/api-documentation/authentication
- **Analytics Token:** https://upstox.com/developer/api-documentation/analytics-token
- **Rate Limiting:** https://upstox.com/developer/api-documentation/rate-limiting

---

## Authentication

Header format: `Authorization: Bearer {access_token}`.

The access token expires daily and a human logs in to renew it (CLAUDE.md
"Integrations"). Upstox also offers a long-lived *analytics* token scoped to
market data and streaming; the adapter accepts either, since both are presented
the same way. Whichever is used is read from the environment at run time and is
stored nowhere else.

---

## Historical Candle Data V3

**Endpoint:** `GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}`

### Path Parameters

| Name | Required | Type | Description |
|------|----------|------|-------------|
| instrument_key | Yes | string | Format: `NSE_EQ\|ISIN` (e.g., `NSE_EQ\|INE848E01016`) |
| unit | Yes | string | `minutes`, `hours`, `days`, `weeks`, `months` |
| interval | Yes | string | Depends on unit: `1-300` (min), `1-5` (hour), `1` (day/week/month) |
| to_date | Yes | string | YYYY-MM-DD (inclusive, inclusive endpoint) |
| from_date | Optional | string | YYYY-MM-DD (start date) |

### Historical Availability & Limits

| Unit | Interval | Available From | Max Retrieval Limit |
|------|----------|----------------|---------------------|
| minutes | 1-15 | Jan 2022 | 1 month |
| minutes | 16-300 | Jan 2022 | 1 quarter |
| hours | 1-5 | Jan 2022 | 1 quarter |
| **days** | **1** | **Jan 2000** | **1 decade** |
| weeks | 1 | Jan 2000 | No limit |
| months | 1 | Jan 2000 | No limit |

**For MVP (daily candles):** `unit=days`, `interval=1`, data back to Jan 2000 with 1 decade max retrieval.

### Request Example

```bash
curl --location 'https://api.upstox.com/v3/historical-candle/NSE_EQ%7CINE848E01016/days/1/2025-03-01/2025-01-01' \
  --header 'Content-Type: application/json' \
  --header 'Accept: application/json' \
  --header 'Authorization: Bearer {your_access_token}'
```

### Response Format

```json
{
  "status": "success",
  "data": {
    "candles": [
      [
        "2025-01-01T00:00:00+05:30",
        53.1,
        53.95,
        51.6,
        52.05,
        235519861,
        0
      ],
      [
        "2025-01-02T00:00:00+05:30",
        50.35,
        56.85,
        49.35,
        52.8,
        1004998611,
        0
      ]
    ]
  }
}
```

### Candle Array Format

Each candle is an array with **exactly 7 elements**:

| Index | Field | Type | Description |
|-------|-------|------|-------------|
| 0 | timestamp | string | ISO 8601 with timezone, e.g., `"2025-01-01T00:00:00+05:30"` |
| 1 | open | number | Opening price (Decimal, can be string in JSON) |
| 2 | high | number | Highest price in period |
| 3 | low | number | Lowest price in period |
| 4 | close | number | Closing price |
| 5 | volume | integer | Total volume traded |
| 6 | open_interest | integer | Open interest (0 for equities) |

**Constraints:**
- All prices must be > 0
- high ≥ low
- high ≥ max(open, close)
- low ≤ min(open, close)
- volume ≥ 0 (can be zero on holidays)
- open_interest ≥ 0

### Error Codes

| Code | Description | Action |
|------|-------------|--------|
| UDAPI1021 | Invalid instrument_key format | Check ISIN, use `NSE_EQ\|{ISIN}` |
| UDAPI1022 | to_date required | Always provide to_date |
| UDAPI100011 | Instrument not found | Verify ISIN is valid/listed |
| UDAPI1015 | Date range invalid | to_date ≥ from_date, YYYY-MM-DD format |
| UDAPI1146 | Invalid unit | Use: minutes, hours, days, weeks, months |
| UDAPI1147 | Invalid interval | Check valid intervals per unit |
| UDAPI1148 | Date range exceeds limit | Within 1 decade for daily data |

---

## Implementation notes

Read by `ingest/upstox/` (see its `adapters.py` and `parser.py` for the
current adapter names, configuration keys and rule version). Two adapters cover
this endpoint: the `official_api` one that calls Upstox directly, and a
`manual_drop` one that re-parses responses saved by hand into the drop folder.
Both store the raw response bytes before parsing and validate with a strict
Pydantic model, so a changed field fails loudly rather than being coerced.

Facts about the endpoint that shape the adapter:

- Daily candles reach back to Jan 2000 with a one-decade retrieval limit, so a
  full history is a handful of requests and needs no pagination.
- Timestamps come back in IST with an offset; they are parsed as timezone-aware
  and never naive.
- Zero volume is valid (holidays and halts) and is stored as returned.
- Open interest is 0 for equities.
- Corporate actions are not in the response. Whether the vendor has already
  adjusted a series is *checked* against the exchange bhavcopy, never assumed —
  see `upstox_bhavcopy_crosscheck_as_of` in `core/db/pit.py`.

Prices are read back through the point-in-time functions in `core/db/pit.py`
(`upstox_candles_as_of` and the quarantine reads beside it), never by querying
the table directly.
