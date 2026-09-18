# Corporate-action fixtures: written by hand, not recorded

`nse_corporate_actions_sample.csv` follows the column layout of NSE's
corporate-actions export (nseindia.com, Corporate Filings > Corporate
Actions > Download .csv) as remembered on 2026-09-18. It was **not** checked
against a real download. The rows are illustrative and are not real
announcements.

The rows cover:
- a bonus, a dividend, a split and a rights issue at a premium;
- an AGM row that also names a dividend;
- rows that are out of scope: an AGM, and a non-equity series;
- a demerger, whose ratio is not in the exchange field;
- a bonus with no ratio;
- an impossible date.

**Replace this with a real export** before relying on the parser. Download one
by hand, add it here byte-for-byte, and fix `ingest/corporate_actions/parser.py`
wherever the real layout differs. A difference in header or date format
fails the whole file loudly (`shape_changed` or `malformed_row`).

`curated_sample.csv` has the exact columns of the curated file
(`CURATED_COLUMNS`). Its evidence URLs are placeholders.
