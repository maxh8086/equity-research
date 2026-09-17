"""Contract tests for the NSE Indices constituent-list parser, on recorded files."""

import csv
from pathlib import Path

import pytest

from core.db.models import IndexCode, QuarantineReason
from ingest.nse_indices.parser import ListRejected, index_for_url, parse_constituent_list

FIXTURES = Path(__file__).parent / "fixtures" / "nse_indices"
NOW = (FIXTURES / "ind_nifty50list_20260915.csv").read_bytes()
FIELDS = ["company_name", "industry", "symbol", "series", "isin"]


@pytest.mark.parametrize(
    ("name", "index_code", "count"),
    [
        ("ind_nifty50list_20160310050302.csv", IndexCode.NIFTY_50, 50),
        ("ind_nifty50list_20171016083142.csv", IndexCode.NIFTY_50, 50),
        ("ind_nifty50list_20260915.csv", IndexCode.NIFTY_50, 50),
        ("ind_niftynext50list_20260915.csv", IndexCode.NIFTY_NEXT_50, 50),
    ],
)
def test_recorded_lists_parse_without_issues(name, index_code, count):
    parsed = parse_constituent_list((FIXTURES / name).read_bytes(), index_code)
    assert (len(parsed.constituents), parsed.issues, parsed.index_code) == (count, (), index_code)


def test_recorded_2016_list_first_row():
    parsed = parse_constituent_list((FIXTURES / "ind_nifty50list_20160310050302.csv").read_bytes(), IndexCode.NIFTY_50)
    first = parsed.constituents[0]
    assert (first.row_number, first.row.company_name, first.row.symbol, first.row.series, first.row.isin) == (
        1, "ACC Ltd.", "ACC", "EQ", "INE012A01025"
    )  # fmt: skip


def _lines() -> list[str]:
    return NOW.decode().splitlines()


def _data(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _replace(lines: list[str], row_number: int, **fields: str) -> list[str]:
    parts = next(csv.reader([lines[row_number]]))
    for key, value in fields.items():
        parts[FIELDS.index(key)] = value
    return [*lines[:row_number], ",".join(parts), *lines[row_number + 1 :]]


def test_bom_crlf_and_blank_lines_are_tolerated():
    data = b"\xef\xbb\xbf" + NOW.replace(b"\n", b"\r\n") + b"\r\n\r\n"
    assert parse_constituent_list(data, IndexCode.NIFTY_50) == parse_constituent_list(NOW, IndexCode.NIFTY_50)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (_data([_lines()[0].replace("ISIN Code", "ISIN"), *_lines()[1:]]), QuarantineReason.SHAPE_CHANGED),
        (_data([_lines()[0] + ",Weightage", *(r + ",1.0" for r in _lines()[1:])]), QuarantineReason.SHAPE_CHANGED),
        (b"\n", QuarantineReason.SHAPE_CHANGED),
        (b"\xff\xfe" + NOW, QuarantineReason.SHAPE_CHANGED),
        (_data(_lines()[:30]), QuarantineReason.ROW_COUNT),
        (_data([*_lines(), _lines()[1]]), QuarantineReason.DUPLICATE_ISIN),
    ],
    ids=["renamed-column", "added-column", "empty", "not-utf8", "truncated", "duplicate-isin"],
)
def test_whole_file_rejected(data, reason):
    with pytest.raises(ListRejected) as exc:
        parse_constituent_list(data, IndexCode.NIFTY_50)
    assert exc.value.reason is reason


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"isin": "INE002A01019"}, QuarantineReason.INVALID_ISIN),  # check digit
        ({"isin": "US0378331005"}, QuarantineReason.INVALID_ISIN),  # valid, not Indian
        # BZ is a real NSE trade-for-trade series, distinct from BE, with no confirmed
        # constituent yet (unlike BE -- see RULE_VERSION history) -- still quarantined.
        ({"series": "BZ"}, QuarantineReason.NON_EQUITY_SERIES),
        ({"symbol": ""}, QuarantineReason.MALFORMED_ROW),
        ({"company_name": ""}, QuarantineReason.MALFORMED_ROW),
    ],
    ids=["check-digit", "foreign-isin", "series", "no-symbol", "no-name"],
)
def test_bad_row_quarantined_and_the_rest_kept(fields, reason):
    parsed = parse_constituent_list(_data(_replace(_lines(), 7, **fields)), IndexCode.NIFTY_50)
    [issue] = parsed.issues
    assert (issue.row_number, issue.reason) == (7, reason)
    assert len(parsed.constituents) == 49
    assert 7 not in {c.row_number for c in parsed.constituents}


def test_be_series_is_accepted_alongside_eq():
    """Confirmed real constituents can trade in BE (trade-for-trade) -- see RULE_VERSION."""
    parsed = parse_constituent_list(_data(_replace(_lines(), 7, series="BE")), IndexCode.NIFTY_50)
    assert parsed.issues == () and len(parsed.constituents) == 50
    assert parsed.constituents[6].row.series == "BE"


def test_row_with_wrong_field_count_quarantined():
    lines = _lines()
    lines[3] += ",extra"
    [issue] = parse_constituent_list(_data(lines), IndexCode.NIFTY_50).issues
    assert (issue.row_number, issue.reason, issue.raw_row) == (3, QuarantineReason.MALFORMED_ROW, lines[3])


@pytest.mark.parametrize(
    ("url", "index_code"),
    [
        ("https://archives.nseindia.com/content/indices/ind_nifty50list.csv", IndexCode.NIFTY_50),
        ("https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv", IndexCode.NIFTY_NEXT_50),
        ("https://web.archive.org/web/20171016083142id_/http://niftyindices.com:80/IndexConstituent/ind_nifty50list.csv", IndexCode.NIFTY_50),
        ("https://archives.nseindia.com/content/indices/ind_nifty100list.csv", None),
        ("https://example.in/ind_nifty50list.csv.bak", None),
    ],
)  # fmt: skip
def test_index_for_url(url, index_code):
    assert index_for_url(url) is index_code
