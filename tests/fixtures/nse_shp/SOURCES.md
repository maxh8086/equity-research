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

## Publication times

Looked up on 2026-09-18 on NSE's shareholding-pattern page
(`https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern`,
column "Broadcast date/time"; the page's data comes from
`/api/corporate-share-holdings-master?index=equities&symbol=<SYMBOL>`), matched
to each file by its `xbrl` link.

| File | Submitted | Revised | Broadcast (IST) |
|---|---|---|---|
| `SHP_162513_538030_19102021032428_WEB.xml` | 19-Oct-2021 | - | 07-Jan-2022 00:10:01 |
| `SHP_1574385_13112025090903_WEB.xml` | 08-Oct-2025 | - | 08-Oct-2025 14:20:05 |
| `SHP_1584868_11122025040253_WEB.xml` | 11-Dec-2025 | - | 11-Dec-2025 16:02:58 |
| `SHP_1660737_28042026112844_WEB.xml` | 21-Apr-2026 | 28-Apr-2026 | 06-May-2026 13:05:14 |
| `SHP_1687801_03072026023346_WEB.xml` | 03-Jul-2026 | - | 03-Jul-2026 14:33:54 |
| `SHP_1690812_10072026084030_WEB.xml` | 10-Jul-2026 | - | 10-Jul-2026 20:40:37 |
| `SHP_1693581_15072026065242_WEB.xml` | 15-Jul-2026 | - | 15-Jul-2026 18:52:47 |
| `SHP_1695339_17072026045502_WEB.xml` | 17-Jul-2026 | - | 17-Jul-2026 16:55:08 |
| `SHP_1696479_20072026120029_WEB.xml` | 20-Jul-2026 | - | 20-Jul-2026 12:00:34 |
| `SHP_1698632_21072026062045_WEB.xml` | 21-Jul-2026 | - | 21-Jul-2026 18:20:54 |

What the listing shows about itself:

- The broadcast time is a few seconds after the file-name clock read as PM
  (or noon, for VEDL's `12`). The file-name rule in the adapter holds.
- **HDFCBANK 30-Sep-2025 does not fit.** Its file name says 13-Nov-2025
  09:09:03 (AM or PM), five weeks after the listed broadcast of 08-Oct-2025.
  The file was replaced without a revision flag, so the listed broadcast time
  is not when these bytes became public. With that `published_at` the adapter
  rejects the file as `implausible_as_of`, which is correct: no earlier than
  13-Nov-2025 09:09:03 is known.
- **HDFCLIFE is the revised file** (28-Apr-2026: "Inadvertent error in
  calculating outstanding ESOPs number"). The listing no longer links the
  original 21-Apr-2026 file. A listing adapter must keep every version of the
  listing it sees, or originals are lost.
- **INFY 30-Sep-2021** lists a broadcast in January 2022, 80 days after
  submission, with no system time. It looks like a later reload of the
  listing. Using it as `as_of` is late, never early, so it is safe (R2).
- The listing's `isin` field is not the equity ISIN for several companies
  (INFY `IN9009A01011`, HDFCBANK `INE040A01018`, VEDL `INE205A01017`,
  INDUSINDBK `IN9095A01010`; the files carry the current equity ISINs). It
  must never be used to resolve an ISIN.

The adapter tests use the listed broadcast times for VEDL and ADANIPORTS.

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
