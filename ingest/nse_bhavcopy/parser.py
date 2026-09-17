"""Parser for NSE's daily bhavcopy, shared by every adapter that reads it.

UDiFF ("Uniform Data Interchange File Format") is NSE's current full
capital-market bhavcopy: one zipped CSV per trade date, all instruments in
the CM segment, columns identified by name (BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv,
verified live at archives.nseindia.com/content/cm/ for 2026-09-16, see
tests/fixtures/nse_bhavcopy/SOURCES.md). It replaced the older per-security
"sec_bhavdata_full" CSV, which never carried ISIN, and before that the
discontinued cm<date>bhav.csv.zip. Column order and count have grown over
NSE's own history (trailing Rsvd1-4 columns are reserved for future use), so
this parser reads by header name, not position, and tolerates unknown extra
columns.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import Field, ValidationError

from core.compute.isin import is_valid_isin
from core.db.models import BhavcopyQuarantineReason
from ingest.schema import StrictModel

RULE_VERSION = "nse_bhavcopy/1"

# Research decision: NSE's UDiFF rollout date. No bhavcopy exists in this
# format before it; a pre-UDIFF historical load needs a different parser.
UDIFF_START_DATE = date(2024, 7, 8)

REQUIRED_COLUMNS = (
    "TradDt", "ISIN", "TckrSymb", "SctySrs",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
    "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd",
)  # fmt: skip

# NSE trading-series codes confirmed to be an ordinary equity, not a reason to
# skip a row. Human-curated and grown only from confirmed evidence (R1),
# mirrors ingest/nse_indices/parser.py's EQUITY_SERIES. Unlike that module,
# a series NOT on this list is not quarantined here: bhavcopy is the whole
# exchange's book (SME, debt, gilts, gold bonds, odd-lot and trade-to-trade
# sub-series), and most of every day's ~1000 non-equity rows are routine, not
# anomalous. Only rows already claiming to be EQ/BE get scrutinised further.
EQUITY_SERIES = frozenset({"EQ", "BE"})

# The file's own book of the whole exchange: outside this band on either
# count the file is truncated, mixes in another segment, or is not what it
# claims to be. Observed 2026-09-16: 3670 total rows, 2886 EQ+BE. Generous
# headroom for market growth over the years this system will run.
MIN_TOTAL_ROWS = 1000
MAX_TOTAL_ROWS = 10000
MIN_EQUITY_ROWS = 1000
MAX_EQUITY_ROWS = 5000

_DECIMAL_RE = r"^\d+(\.\d+)?$"
_INT_RE = r"^\d+$"


class BhavcopyRawRow(StrictModel):
    isin: str = Field(pattern=r"^[A-Z0-9]{12}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9&_\-]*$")
    series: str = Field(pattern=r"^[A-Z0-9]{1,2}$")
    open: str = Field(pattern=_DECIMAL_RE)
    high: str = Field(pattern=_DECIMAL_RE)
    low: str = Field(pattern=_DECIMAL_RE)
    close: str = Field(pattern=_DECIMAL_RE)
    prev_close: str = Field(pattern=_DECIMAL_RE)
    volume: str = Field(pattern=_INT_RE)
    turnover: str = Field(pattern=_DECIMAL_RE)
    trades: str = Field(pattern=_INT_RE)


@dataclass(frozen=True)
class BhavcopyRow:
    row_number: int  # 1-based among non-blank data rows, all instruments
    trade_date: date
    isin: str
    symbol: str
    series: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume: int
    turnover: Decimal
    trades: int


@dataclass(frozen=True)
class RowIssue:
    row_number: int
    raw_row: str
    reason: BhavcopyQuarantineReason
    detail: str
    isin: str | None = None
    symbol: str | None = None
    series: str | None = None


@dataclass(frozen=True)
class ParsedBhavcopy:
    trade_date: date
    rows: tuple[BhavcopyRow, ...]
    issues: tuple[RowIssue, ...]


class BhavcopyRejected(Exception):
    """The whole file is unusable. Its raw bytes are kept; nothing else is stored."""

    def __init__(self, reason: BhavcopyQuarantineReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _extract_csv(data: bytes) -> bytes:
    """The bhavcopy CSV, unzipping first when `data` is the archive's own zip."""
    if data[:4] != b"PK\x03\x04":
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise BhavcopyRejected(
                    BhavcopyQuarantineReason.SHAPE_CHANGED,
                    f"zip has {len(zf.namelist())} member(s), expected exactly one .csv",
                )
            return zf.read(names[0])
    except zipfile.BadZipFile as exc:
        raise BhavcopyRejected(BhavcopyQuarantineReason.SHAPE_CHANGED, f"not a valid zip: {exc}") from None


def _fields(line: str) -> list[str]:
    return next(csv.reader([line]), [])


def parse_bhavcopy(data: bytes) -> ParsedBhavcopy:
    try:
        text = _extract_csv(data).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BhavcopyRejected(BhavcopyQuarantineReason.SHAPE_CHANGED, f"not UTF-8: {exc}") from None

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise BhavcopyRejected(BhavcopyQuarantineReason.SHAPE_CHANGED, "empty file")
    header = _fields(lines[0])
    if missing := [c for c in REQUIRED_COLUMNS if c not in header]:
        raise BhavcopyRejected(BhavcopyQuarantineReason.SHAPE_CHANGED, f"missing columns {missing}")

    body = lines[1:]
    if not MIN_TOTAL_ROWS <= len(body) <= MAX_TOTAL_ROWS:
        raise BhavcopyRejected(
            BhavcopyQuarantineReason.ROW_COUNT,
            f"{len(body)} rows, expected {MIN_TOTAL_ROWS}-{MAX_TOTAL_ROWS}",
        )

    trade_dates = {_fields(line)[header.index("TradDt")].strip() for line in body if len(_fields(line)) == len(header)}
    if len(trade_dates) != 1:
        raise BhavcopyRejected(
            BhavcopyQuarantineReason.SHAPE_CHANGED, f"file mixes trade dates {sorted(trade_dates)[:5]}"
        )
    try:
        trade_date = datetime.strptime(next(iter(trade_dates)), "%Y-%m-%d").date()
    except ValueError as exc:
        raise BhavcopyRejected(BhavcopyQuarantineReason.SHAPE_CHANGED, f"TradDt not YYYY-MM-DD: {exc}") from None

    rows: list[BhavcopyRow] = []
    issues: list[RowIssue] = []
    seen: dict[tuple[str, str], int] = {}
    equity_count = 0
    for number, line in enumerate(body, start=1):
        fields = _fields(line)
        if len(fields) != len(header):
            issues.append(
                RowIssue(number, line, BhavcopyQuarantineReason.MALFORMED_ROW, f"{len(fields)} fields, expected {len(header)}")
            )  # fmt: skip
            continue
        raw = dict(zip(header, fields))
        series = raw["SctySrs"].strip()
        if series not in EQUITY_SERIES:
            continue  # the rest of the market's instruments: out of scope, not an anomaly
        equity_count += 1
        try:
            parsed = BhavcopyRawRow(
                isin=raw["ISIN"].strip(),
                symbol=raw["TckrSymb"].strip(),
                series=series,
                open=raw["OpnPric"].strip(),
                high=raw["HghPric"].strip(),
                low=raw["LwPric"].strip(),
                close=raw["ClsPric"].strip(),
                prev_close=raw["PrvsClsgPric"].strip(),
                volume=raw["TtlTradgVol"].strip(),
                turnover=raw["TtlTrfVal"].strip(),
                trades=raw["TtlNbOfTxsExctd"].strip(),
            )
        except ValidationError as exc:
            error = exc.errors()[0]
            detail = f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
            issues.append(
                RowIssue(number, line, BhavcopyQuarantineReason.MALFORMED_ROW, detail,
                          isin=raw.get("ISIN", "").strip() or None,
                          symbol=raw.get("TckrSymb", "").strip() or None, series=series)
            )  # fmt: skip
            continue
        if not is_valid_isin(parsed.isin):
            issues.append(
                RowIssue(number, line, BhavcopyQuarantineReason.INVALID_ISIN, f"{parsed.isin} is not a valid ISIN",
                          isin=parsed.isin, symbol=parsed.symbol, series=series)
            )  # fmt: skip
            continue
        key = (parsed.isin, series)
        if key in seen:
            # Should never happen -- this file is NSE's own book of record for the day.
            issues.append(
                RowIssue(number, line, BhavcopyQuarantineReason.DUPLICATE_ROW,
                          f"{parsed.isin}/{series} also on row {seen[key]}",
                          isin=parsed.isin, symbol=parsed.symbol, series=series)
            )  # fmt: skip
            continue
        seen[key] = number
        rows.append(
            BhavcopyRow(
                row_number=number,
                trade_date=trade_date,
                isin=parsed.isin,
                symbol=parsed.symbol,
                series=series,
                open=Decimal(parsed.open),
                high=Decimal(parsed.high),
                low=Decimal(parsed.low),
                close=Decimal(parsed.close),
                prev_close=Decimal(parsed.prev_close),
                volume=int(parsed.volume),
                turnover=Decimal(parsed.turnover),
                trades=int(parsed.trades),
            )
        )

    if not MIN_EQUITY_ROWS <= equity_count <= MAX_EQUITY_ROWS:
        raise BhavcopyRejected(
            BhavcopyQuarantineReason.ROW_COUNT,
            f"{equity_count} equity rows, expected {MIN_EQUITY_ROWS}-{MAX_EQUITY_ROWS}",
        )
    return ParsedBhavcopy(trade_date, tuple(rows), tuple(issues))
