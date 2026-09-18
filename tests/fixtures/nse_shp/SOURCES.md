# Shareholding-pattern fixtures: real filings, recorded byte-for-byte

Ten SEBI Regulation 31 shareholding-pattern XBRL instances, downloaded by
2026-09-18 from NSE's archive host. Each file's URL is:

    https://nsearchives.nseindia.com/corporate/xbrl/<file name>

Each file is unchanged: no reformatting and no edits. Tests that need a broken
or revised file mutate these bytes in memory.

| File | Company | ISIN in file | Taxonomy | As on | File-name clock |
|---|---|---|---|---|---|
| `SHP_162513_538030_19102021032428_WEB.xml` | INFY | INE009A01021 | 2020-09-30 | 2021-09-30 | 19-10-2021 03:24:28 |
| `SHP_1574385_13112025090903_WEB.xml` | HDFCBANK | INE040A01034 | 2025-05-31 | 2025-09-30 | 13-11-2025 09:09:03 |
| `SHP_1584868_11122025040253_WEB.xml` | INFY (allotment, 2025-12-04) | INE009A01021 | 2025-05-31 | 2025-12-04 | 11-12-2025 04:02:53 |
| `SHP_1660737_28042026112844_WEB.xml` | HDFCLIFE | INE795G01014 | 2025-10-31 | 2026-03-31 | 28-04-2026 11:28:44 |
| `SHP_1687801_03072026023346_WEB.xml` | HDFCBANK | INE040A01034 | 2025-10-31 | 2026-06-30 | 03-07-2026 02:33:46 |
| `SHP_1690812_10072026084030_WEB.xml` | ADANIPORTS | INE742F01042 | 2025-10-31 | 2026-06-30 | 10-07-2026 08:40:30 |
| `SHP_1693581_15072026065242_WEB.xml` | INFY | INE009A01021 | 2025-10-31 | 2026-06-30 | 15-07-2026 06:52:42 |
| `SHP_1695339_17072026045502_WEB.xml` | INDUSINDBK | INE095A01012 | 2025-10-31 | 2026-06-30 | 17-07-2026 04:55:02 |
| `SHP_1696479_20072026120029_WEB.xml` | VEDL | INE205A01025 | 2025-10-31 | 2026-06-30 | 20-07-2026 12:00:29 |
| `SHP_1698632_21072026062045_WEB.xml` | JSWSTEEL | INE019A01038 | 2025-10-31 | 2026-06-30 | 21-07-2026 06:20:45 |

## Publication time was not recorded

NSE's broadcast time for these filings was not written down when they were
downloaded. The file name's clock is 12-hour with no AM/PM marker, so it gives
only a lower bound (the adapter reads it as AM, `generated_no_earlier_than`).
The adapter tests therefore use an **assumed** `published_at`, at or after the
file-name clock read as PM. Those times are test inputs, not facts about the
filings. For real loads, take `published_at` from the broadcast time on NSE's
shareholding-pattern page.

## Figures checked against the published patterns

- INFY, 30-Sep-2021: promoter group 551,682,338 shares; public 3,638,941,503;
  total 4,205,464,426.
- HDFCBANK has no promoter group (both files).
- INDUSINDBK promoter pledged 50,267,535; JSWSTEEL promoter pledged
  125,650,440.
- VEDL promoter encumbered 2,139,651,763 = non-disposal undertakings
  107,342,705 + other encumbrances 2,032,309,058, with no pledge.

## Quirks the parser relies on or tolerates

- Every parent category equals the sum of its children for every measure
  (an absent child counts as zero). A file that fails this is rejected as
  `totals_mismatch`, never partly loaded.
- Percentages are tagged alongside the counts. They are skipped and counted;
  code recomputes them from the counts.
- Named holders (typed dimensions: each promoter, each holder above 1%) are
  counted on `shareholding_filing` and deferred.
- VEDL identifies itself by scrip code only, so the symbol is not
  cross-checked inside the file.
- The 2020 and 2025 layouts split the public differently, so a category keeps
  its own key per layout (`fpi` in 2020, `fpi_category_1` in 2025).
