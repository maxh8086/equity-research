"""Contract tests for the NSE bhavcopy parser, on a recorded file (tests/fixtures/nse_bhavcopy)."""

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from core.compute.isin import is_valid_isin
from core.db.models import BhavcopyQuarantineReason
from ingest.nse_bhavcopy.parser import EQUITY_SERIES, BhavcopyRejected, parse_bhavcopy

FIXTURES = Path(__file__).parent / "fixtures" / "nse_bhavcopy"
ZIP = (FIXTURES / "BhavCopy_NSE_CM_0_0_0_20260916_F_0000.csv.zip").read_bytes()

with zipfile.ZipFile(io.BytesIO(ZIP)) as _zf:
    CSV = _zf.read(_zf.namelist()[0])

LINES = CSV.decode("utf-8-sig").splitlines()
HEADER = next(csv.reader([LINES[0]]))
RELIANCE_ISIN = "INE002A01018"


def _data(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _row_number(symbol: str) -> int:
    idx = HEADER.index("TckrSymb")
    for number, line in enumerate(LINES[1:], start=1):
        if next(csv.reader([line]))[idx] == symbol:
            return number
    raise ValueError(symbol)


def _replace(lines: list[str], row_number: int, **fields: str) -> list[str]:
    parts = next(csv.reader([lines[row_number]]))
    for key, value in fields.items():
        parts[HEADER.index(key)] = value
    return [*lines[:row_number], ",".join(parts), *lines[row_number + 1 :]]


RELIANCE_ROW = _row_number("RELIANCE")


def test_recorded_bhavcopy_unzips_and_parses() -> None:
    parsed = parse_bhavcopy(ZIP)
    assert parsed.trade_date == date(2026, 9, 16)
    assert len(parsed.rows) == 2886
    assert parsed.issues == ()
    assert all(r.series in EQUITY_SERIES for r in parsed.rows)


def test_raw_csv_without_the_zip_wrapper_parses_identically() -> None:
    assert parse_bhavcopy(CSV) == parse_bhavcopy(ZIP)


def test_reliance_row_matches_the_real_market_data() -> None:
    [reliance] = [r for r in parse_bhavcopy(CSV).rows if r.symbol == "RELIANCE"]
    assert (reliance.isin, reliance.series) == (RELIANCE_ISIN, "EQ")
    assert is_valid_isin(reliance.isin)
    assert reliance.high >= reliance.low >= 0
    assert reliance.volume > 0 and reliance.trades > 0


def test_be_series_row_is_kept() -> None:
    [row] = [r for r in parse_bhavcopy(CSV).rows if r.symbol == "3IINFOLTD"]
    assert row.series == "BE" and row.isin == "INE748C01038"


def test_non_equity_rows_are_filtered_not_quarantined() -> None:
    """Most of the file is SME, debt, gilt and gold-bond instruments: routine, not an anomaly."""
    parsed = parse_bhavcopy(CSV)
    assert len(LINES) - 1 > len(parsed.rows)  # far more total lines than equity rows
    assert parsed.issues == ()


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (_data([LINES[0].replace("TckrSymb", "Symbol"), *LINES[1:]]), BhavcopyQuarantineReason.SHAPE_CHANGED),
        (b"\n", BhavcopyQuarantineReason.SHAPE_CHANGED),
        (b"\xff\xfe" + CSV, BhavcopyQuarantineReason.SHAPE_CHANGED),
        (b"PK\x03\x04not a real zip", BhavcopyQuarantineReason.SHAPE_CHANGED),
        (_data(LINES[:30]), BhavcopyQuarantineReason.ROW_COUNT),
        (
            _data(_replace(LINES, RELIANCE_ROW, TradDt="2026-09-15")),
            BhavcopyQuarantineReason.SHAPE_CHANGED,
        ),
    ],
    ids=["renamed-column", "empty", "not-utf8", "bad-zip-magic", "truncated", "mixed-trade-dates"],
)
def test_whole_file_rejected(data: bytes, reason: BhavcopyQuarantineReason) -> None:
    with pytest.raises(BhavcopyRejected) as exc:
        parse_bhavcopy(data)
    assert exc.value.reason is reason


def test_bad_row_quarantined_and_the_rest_kept() -> None:
    wrong = RELIANCE_ISIN[:-1] + str((int(RELIANCE_ISIN[-1]) + 1) % 10)
    parsed = parse_bhavcopy(_data(_replace(LINES, RELIANCE_ROW, ISIN=wrong)))
    [issue] = parsed.issues
    assert (issue.row_number, issue.reason, issue.symbol) == (RELIANCE_ROW, BhavcopyQuarantineReason.INVALID_ISIN, "RELIANCE")
    assert len(parsed.rows) == 2885
    assert "RELIANCE" not in {r.symbol for r in parsed.rows}


def test_malformed_numeric_field_quarantined_with_partial_identity() -> None:
    parsed = parse_bhavcopy(_data(_replace(LINES, RELIANCE_ROW, OpnPric="not-a-number")))
    [issue] = parsed.issues
    assert (issue.row_number, issue.reason) == (RELIANCE_ROW, BhavcopyQuarantineReason.MALFORMED_ROW)
    assert (issue.isin, issue.symbol, issue.series) == (RELIANCE_ISIN, "RELIANCE", "EQ")


def test_row_with_wrong_field_count_quarantined_without_identity() -> None:
    lines = list(LINES)
    lines[RELIANCE_ROW] += ",extra"
    [issue] = parse_bhavcopy(_data(lines)).issues
    assert (issue.row_number, issue.reason, issue.raw_row) == (RELIANCE_ROW, BhavcopyQuarantineReason.MALFORMED_ROW, lines[RELIANCE_ROW])
    assert (issue.isin, issue.symbol, issue.series) == (None, None, None)


def test_duplicate_isin_series_in_one_file_quarantines_the_later_row() -> None:
    tcs_row = _row_number("TCS")
    assert tcs_row > RELIANCE_ROW  # the file is sorted by symbol; TCS comes after RELIANCE
    lines = _replace(LINES, tcs_row, ISIN=RELIANCE_ISIN, SctySrs="EQ")
    parsed = parse_bhavcopy(_data(lines))
    [issue] = parsed.issues
    assert (issue.row_number, issue.reason, issue.isin) == (tcs_row, BhavcopyQuarantineReason.DUPLICATE_ROW, RELIANCE_ISIN)
    assert RELIANCE_ISIN in {r.isin for r in parsed.rows}  # the first occurrence (RELIANCE) is kept
