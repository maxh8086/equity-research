"""Parser for NSE Indices constituent lists, shared by every adapter that reads them.

Files ind_nifty50list.csv and ind_niftynext50list.csv, served by niftyindices.com
and NSE's archive hosts and captured by the Wayback Machine. The layout has not
changed since at least March 2016 (recorded in tests/fixtures/nse_indices/):

    Company Name,Industry,Symbol,Series,ISIN Code

The file carries no publication date; the adapter supplies `as_of`. Company
names are stored for review only and never used to identify a company.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from core.compute.isin import is_valid_isin
from core.db.models import IndexCode, QuarantineReason
from ingest.schema import StrictModel

# /2: archive digests are checked against the bytes as transferred (gzip), not decoded.
RULE_VERSION = "nse_indices_constituents/2"

HEADER = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")
FIELDS = ("company_name", "industry", "symbol", "series", "isin")

LIST_FILES: dict[str, IndexCode] = {
    "ind_nifty50list.csv": IndexCode.NIFTY_50,
    "ind_niftynext50list.csv": IndexCode.NIFTY_NEXT_50,
}

# Research decisions, not config. Outside this range the file is truncated or is
# not the list it claims to be. The slack allows a list published mid-replacement.
MIN_CONSTITUENTS = 40
MAX_CONSTITUENTS = 60
EQUITY_SERIES = "EQ"

_INDIAN_ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")


class ConstituentRow(StrictModel):
    company_name: str = Field(min_length=1)
    industry: str
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9&_\-]*$")
    series: str = Field(pattern=r"^[A-Z0-9]{1,2}$")
    isin: str = Field(pattern=r"^[A-Z0-9]{12}$")


@dataclass(frozen=True)
class Constituent:
    row_number: int  # 1-based among non-blank data rows
    row: ConstituentRow


@dataclass(frozen=True)
class RowIssue:
    row_number: int
    raw_row: str
    reason: QuarantineReason
    detail: str


@dataclass(frozen=True)
class ParsedList:
    index_code: IndexCode
    constituents: tuple[Constituent, ...]
    issues: tuple[RowIssue, ...]


class ListRejected(Exception):
    """The whole file is unusable. Its raw bytes are kept; nothing else is stored."""

    def __init__(self, reason: QuarantineReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def index_for_url(url: str) -> IndexCode | None:
    """The index a list URL publishes, from its file name. Works for Wayback capture URLs."""
    return LIST_FILES.get(urlsplit(url).path.rsplit("/", 1)[-1])


def _fields(line: str) -> list[str]:
    return [f.strip() for f in next(csv.reader([line]), [])]


def parse_constituent_list(data: bytes, index_code: IndexCode) -> ParsedList:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ListRejected(QuarantineReason.SHAPE_CHANGED, f"not UTF-8: {exc}") from None

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ListRejected(QuarantineReason.SHAPE_CHANGED, "empty file")
    if (header := tuple(_fields(lines[0]))) != HEADER:
        raise ListRejected(QuarantineReason.SHAPE_CHANGED, f"header {header!r}, expected {HEADER!r}")
    body = lines[1:]
    if not MIN_CONSTITUENTS <= len(body) <= MAX_CONSTITUENTS:
        raise ListRejected(
            QuarantineReason.ROW_COUNT,
            f"{len(body)} rows, expected {MIN_CONSTITUENTS}-{MAX_CONSTITUENTS}",
        )

    constituents: list[Constituent] = []
    issues: list[RowIssue] = []
    seen: dict[str, int] = {}
    for number, line in enumerate(body, start=1):
        fields = _fields(line)
        if len(fields) != len(FIELDS):
            issues.append(
                RowIssue(number, line, QuarantineReason.MALFORMED_ROW, f"{len(fields)} fields")
            )
            continue
        try:
            row = ConstituentRow(**dict(zip(FIELDS, fields)))
        except ValidationError as exc:
            error = exc.errors()[0]
            detail = f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            issues.append(RowIssue(number, line, QuarantineReason.MALFORMED_ROW, detail))
            continue
        if not (_INDIAN_ISIN.match(row.isin) and is_valid_isin(row.isin)):
            issues.append(
                RowIssue(number, line, QuarantineReason.INVALID_ISIN, f"{row.isin} is not a valid Indian ISIN")
            )
            continue
        if row.series != EQUITY_SERIES:
            issues.append(
                RowIssue(number, line, QuarantineReason.NON_EQUITY_SERIES, f"series {row.series}")
            )
            continue
        if row.isin in seen:
            raise ListRejected(
                QuarantineReason.DUPLICATE_ISIN, f"{row.isin} on rows {seen[row.isin]} and {number}"
            )
        seen[row.isin] = number
        constituents.append(Constituent(number, row))
    return ParsedList(index_code, tuple(constituents), tuple(issues))
