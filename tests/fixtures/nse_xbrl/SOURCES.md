# XBRL results fixtures: real filings, recorded byte-for-byte

Eight SEBI results XBRL instances, downloaded on 2026-09-18 from NSE's archive
host. Each file's URL is:

    https://nsearchives.nseindia.com/corporate/xbrl/<file name>

Each file is unchanged: no reformatting and no edits. Tests that need a broken
file mutate these bytes in memory.

| File | Company | Taxonomy | Basis | Quarter end | Board meeting | Disseminated (IST) |
|---|---|---|---|---|---|---|
| `INDAS_104589_1099938_19042024112830.xml` | INFY | Ind AS | consolidated | 2024-03-31 (Yearly) | 2024-04-18 | 19-Apr-2024 11:32:06 |
| `INDAS_112850_1276017_17102024074402.xml` | INFY | Ind AS | standalone | 2024-09-30 | 2024-10-17 | 17-Oct-2024 19:44:31 |
| `INDAS_114281_1299108_06112024065731.xml` | TATASTEEL | Ind AS | consolidated | 2024-09-30 | 2024-11-06 | 06-Nov-2024 18:58:05 |
| `NBFC_INDAS_113038_1285327_22102024074026.xml` | BAJFINANCE | NBFC (Ind AS) | standalone | 2024-09-30 | 2024-10-22 | 22-Oct-2024 19:41:07 |
| `BANKING_112946_1281172_20102024121424.xml` | HDFCBANK | Banking 2019 | consolidated | 2024-09-30 | 2024-10-19 | 20-Oct-2024 12:14:30 |
| `BANKING_117524_1359008_23012025122553.xml` | HDFCBANK | Banking 2019 | standalone | 2024-12-31 | 2025-01-22 | 23-Jan-2025 12:26:07 |
| `LI_112794_1271828_16102024124453.xml` | HDFCLIFE | Life insurance | standalone | 2024-09-30 | 2024-10-15 | 16-Oct-2024 12:45:30 |
| `GI_117338_1350247_17012025074725.xml` | ICICIGI | General insurance | standalone | 2024-12-31 | 2025-01-17 | 17-Jan-2025 19:47:39 |

The dissemination time is the `exchdisstime` field of NSE's
financial-results listing for that filing. It is the `published_at` used in the
tests. The Ind AS and NBFC files carry no ISIN. The bank and insurer files do.

## Figures checked against the published results

- INFY FY24 consolidated revenue from operations: ₹1,53,670 crore for the
  year and ₹37,923 crore for Q4.
- HDFCBANK Q3 FY25 standalone net profit: ₹16,735.50 crore.

## Quirks the parser relies on or tolerates

- Only columns One (quarter) and Four (year to date) appear. The XBRL period
  of `FourD` repeats the quarter's dates. The reporting period comes from
  `DateOfStartOfReportingPeriod`/`DateOfEndOfReportingPeriod`.
- `decimals="-7"` appears alongside values stated to 0.01 crore, so
  `decimals` is not checked against the value.
- Seven of the eight files tag two different values with the same element and
  context: opening and closing cash under `CashAndCashEquivalentsCashFlowStatement`
  in `FourD`, or the general insurer's two appropriation lines. Both copies are
  quarantined as `conflicting_values`; nothing is picked.
- Results XBRL has no gross block. It has net property, plant and equipment
  plus CWIP, and only in half-yearly and yearly balance sheets. Session 7
  (CWIP → gross block) needs another source.
