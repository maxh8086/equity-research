# Upstox fixtures: documented shape, not recorded

No Upstox API app exists yet, so these responses were **written by hand**
to match the response shape documented at
<https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/>
(checked 2026-09-18). The prices are illustrative, not real market data.

| File | Request it stands for |
|---|---|
| `historical_candle_NSE_EQ_INE002A01018_days_1_2026-09-16_2026-09-10.json` | `GET https://api.upstox.com/v3/historical-candle/NSE_EQ%7CINE002A01018/days/1/2026-09-16/2026-09-10` |

It follows the documented example:
- rows newest first;
- ISO 8601 timestamps at midnight `+05:30`;
- `[timestamp, open, high, low, close, volume, open_interest]`;
- prices as JSON numbers, some written without a decimal point.

**Replace this with a recorded response** once an API app and a token exist.
Record it byte-for-byte, add it to the table above, and keep this hand-written
file only if a test still needs a case the recording lacks. If the recording's
shape differs, the parser is wrong. Do not edit the recording to fit the parser.
