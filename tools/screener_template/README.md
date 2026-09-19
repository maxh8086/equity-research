# Merged Screener Excel template

`build.py` generates one Screener.in custom Excel template that merges three
templates the family used:

| Code | Source | Kept for |
|---|---|---|
| `VM` | Dr Vijay Malik Screener Excel Template V2.0 (paid product: personal use, never committed) | SSGR, incremental ROE, value per ₹ retained, notional interest, costs as % of sales, P/E history, CAGRs |
| `TF` | Technofunda Investing Screener Excel Template v3 | statement layouts, common size, DuPont, CFO/PAT, capex and FCF |
| `AN` | Analysis.xlsx | quarterly QoQ and YoY growth, "latest is the 10-quarter high" flags |
| `SYS` | this system's rule `ratios/1` (`core/compute/ratios.py`) | the same ratios the Session 6 validation compares |

The output is for reading in Excel. **It is not part of the system (R1):** the
Session 6 validation reads the raw numbers in a Screener export's `Data Sheet`
and computes every ratio in code. No formula in this workbook feeds a store.
The source workbooks are not committed.

## Build and check

```bash
python tools/screener_template/build.py --base "<Technofunda template>.xlsx" --out Merged-Screener-Template-v1.xlsx
powershell -ExecutionPolicy Bypass -File tools/screener_template/verify.ps1 -Path Merged-Screener-Template-v1.xlsx
```

- `--base` is any Screener template. Only its `Data Sheet` is kept, byte for
  byte; the build stops if that sheet's column-A labels differ from `DS_ROWS`.
- `verify.ps1` needs Excel on Windows. It recalculates the workbook and fails
  on two conditions: Excel cannot open the file without repair, or any formula
  shows an error. It also prints spot values for the sample company (Vinati
  Organics, FY13–FY22) to compare with the previous build.

Values from build `merged-1`, to compare against after a rebuild:

| Item | Value |
|---|---|
| Operating profit FY22 | 435.48 (checked by hand from the Data Sheet) |
| OPM FY22 / TTM | 27.0% / 26.5% |
| ROE FY22 | 20.6% |
| RoACE FY22 | 26.6% |
| SSGR FY22 | 31.0% |
| P/E (TTM) | 50.88x |
| Value per ₹ retained | 12.40x |
| DuPont check | OK |

## Layout

Sheets, in order:

1. Summary
2. Income Statement
3. Quarterly
4. Balance Sheet
5. Cash Flow
6. Ratios
7. DuPont
8. System ratios
9. Notes
10. Data Sheet

On the annual sheets, columns B–K hold the ten years. The other columns are:

- **L:** trailing four quarters (TTM), left blank unless all four quarters are reported.
- **M:** trend type.
- **N/O/P:** 9Y, 5Y and 3Y CAGR, or 10/5/3-year averages, or Σ÷Σ ratios.
- **R:** notes.

The Notes sheet lists every row with its source and its definition.

## How the generator is organised

- **`META` and `DS_ROWS`** are Screener's Data Sheet layout (version 2.1). A
  layout change from Screener touches only these two tables.
- **`Row(...)` definitions,** one per line of output, are grouped into `Sheet`s.
  A formula is a template with tokens:
  - `{ds:sales}`: the Data Sheet cell in the same column
  - `{ttm:q_sales}`: the sum of the last four quarters
  - `{meta:cmp}`: a fixed Data Sheet cell
  - `{op}`: a row on the same sheet
  - `{BS.net_worth}`: a row on another sheet
  - `{S.cost_of_funds}`: a Summary cell
  - `{rng:IS.retained@-3:-1}`: a range
  - `@-1` means the previous column; `@K` means a fixed column
  - A reference before column B leaves the cell blank.
- Computed cells are wrapped in `IFERROR(...,"n/m")`. Rows marked `raw=True` are
  plain links.
- `src` records where each row came from. `note` is shown beside the row and on
  the Notes sheet.

## Merging a new template version

When a new or revised template arrives:

1. **Dump it.** List every sheet with cell formulas (openpyxl,
   `load_workbook(path)` without `data_only`). Keep the dump in a scratch
   folder, not the repo.
2. **Check its Data Sheet.** Compare its version (B2/B3) and its column-A labels
   with `DS_ROWS`. If Screener moved rows, update `DS_ROWS` first and rebuild
   before anything else.
3. **Classify each metric** against the Notes row map:
   - *same*: already present. Keep the existing row. If the new wording is
     clearer, update the label.
   - *better definition*: replace the formula. Record the old definition in
     `note` and under "Fixes made in the merge" in `NOTES_TEXT`.
   - *new*: add a `Row` in the right section with a new `src` code. Add that
     code to `SOURCES`.
   - *conflicting*: two defensible definitions. Keep both, with labels that say
     how they differ (see "EBIT ÷ average total assets" next to RoACE).
   - *dropped*: GOOGLEFINANCE, portfolio tracking, charts, or data that is not
     in the Data Sheet. List it under "Left out, and why".
4. **Rebuild and verify.** `verify.ps1` must report no error cells. Compare the
   spot values with the table above. Any change must be explained by a
   definition you changed on purpose.
5. **Bump `TEMPLATE_VERSION`,** add a line to the changelog below, and commit
   `build.py` and this README. Never commit the source workbooks.

## Improvement plan

Planned for later versions, in rough order:

- **Banks, NBFCs and insurers.** Add a lender sheet: NIM proxy (interest
  earned − interest expended over average assets), cost-to-income and ROA.
  Show it only when the sales line holds interest income. Until then, the
  bank caveat on Summary and Notes applies.
- **Charts.** Add charts for sales/OP/PAT, margins, ROE/RoACE and CFO vs PAT
  (openpyxl `BarChart`/`LineChart`, defined as data like the rows).
- **Conditional formatting.** Use VM's bands (PBT/NFA, CFO/PAT, SSGR against
  sales CAGR) as coloured flags instead of notes.
- **Lease liabilities and minority interest.** These are the known gaps against
  the system's `ratios/1`, which Screener's lines cannot show. The System ratios
  sheet notes them; the Session 6 validation report quantifies them per company.
- **A template diff helper.** Write `dump.py`, which writes a normalised list of
  every formula in a template so two versions can be diffed (step 1 above).

## Changelog

- `merged-1` (2026-09-19): first merge of VM V2.0, Technofunda v3 and
  Analysis.xlsx. Fixes:
  - ROCE now uses capital employed, not total assets.
  - Reported PBT is kept.
  - Negative OPM is shown.
  - VM's `#REF!` cell (S41) is dropped.
  - DuPont uses averages, with a tolerance check.
  - Errors are wrapped as "n/m".
