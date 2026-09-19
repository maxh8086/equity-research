# Screener export fixture: rebuilt, not a recorded download

`vinati_data_sheet_v2_1.xlsx` is a one-sheet workbook ("Data Sheet" only)
rebuilt cell by cell from the Data Sheet of the Technofunda Screener template's
Vinati Organics example (Screener layout v2.1, latest period Sep 2022). The
template's other sheets, formulas and formatting are not included. Nothing
from the paid Dr Vijay Malik template is in this repository, and nothing from
it may be committed.

The values are Screener's own numbers for Vinati Organics, as the example
shows them (crore; share count in shares; face value in rupees per share).
The parser reads them into 431 values, with FY22 sales of 1615.51 crore.

**Replace this with a real export** once one is downloaded. Use
"Export to Excel" on https://www.screener.in/company/VINATIORGA/consolidated/,
add the file byte-for-byte, and update the values in
`tests/test_screener_export_parser.py`. A real export may have more sheets:
the parser reads only the Data Sheet. If the layout differs, the parser
rejects the file loudly; fix `ingest/screener_export/parser.py` to match.
