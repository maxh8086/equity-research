"""Parser for Upstox API v3 historical daily candles, shared by every adapter that reads them.

Endpoint (https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/):
GET {base}/historical-candle/{instrument_key}/days/1/{to_date}/{from_date},
both dates inclusive, at most one decade per request for the `days` unit,
data from January 2000. The instrument key is keyed by ISIN
(`NSE_EQ|INE848E01016`), never by ticker. Response:

    {"status": "success",
     "data": {"candles": [["2025-01-01T00:00:00+05:30", open, high, low, close, volume, oi], ...]}}

Built against the documented shape until an API app exists (see
tests/fixtures/upstox/SOURCES.md). Row order is not documented, so nothing
here relies on it. JSON numbers are read as Decimal, never float.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote, unquote

from pydantic import Field, ValidationError

from core.compute.isin import is_valid_isin
from core.db.models import UpstoxQuarantineReason
from core.timezones import IST, require_aware
from ingest.schema import StrictModel

RULE_VERSION = "upstox_candles/1"

EXCHANGE_SEGMENT = "NSE_EQ"

# Upstox's daily history starts in January 2000. Requests are aligned to fixed
# decades from here so a past window's URL never changes: once fetched, it is
# settled and never requested again.
HISTORY_START = date(2000, 1, 1)
WINDOW_YEARS = 10

# Research decision: a day's candle is treated as final only after 18:00 IST
# (the close is 15:30; closing prices settle after that). A fetch before this
# asks only up to the previous day, and a candle dated later than the last
# complete day is quarantined, so a live partial bar is never stored as a day.
DAY_COMPLETE_AFTER = time(18, 0)

_URL_KEY = re.compile(rf"/historical-candle/{EXCHANGE_SEGMENT}\|(?P<isin>[A-Z0-9]{{12}})/days/1/")
_MIDNIGHT_IST = r"^\d{4}-\d{2}-\d{2}T00:00:00\+05:30$"
CANDLE_FIELDS = 7


def instrument_key(isin: str) -> str:
    return f"{EXCHANGE_SEGMENT}|{isin}"


def candles_url(base_url: str, isin: str, from_date: date, to_date: date) -> str:
    key = quote(instrument_key(isin), safe="")
    return f"{base_url.rstrip('/')}/historical-candle/{key}/days/1/{to_date.isoformat()}/{from_date.isoformat()}"


def isin_from_url(url: str) -> str | None:
    """The ISIN in a candles URL, or None if it is not an NSE equity daily-candles URL."""
    match = _URL_KEY.search(unquote(url))
    return match["isin"] if match else None


def last_complete_day(fetched_at: datetime) -> date:
    """The latest trade date whose daily candle is final at `fetched_at`."""
    local = require_aware(fetched_at, "fetched_at").astimezone(IST)
    return local.date() if local.time() >= DAY_COMPLETE_AFTER else local.date() - timedelta(days=1)


def request_windows(end: date, resume_after: date | None) -> list[tuple[date, date]]:
    """(from, to) windows covering HISTORY_START..end, aligned to fixed decades.

    Each window starts the day after `resume_after` (the latest candle already
    stored) when that falls inside it; windows wholly before it are dropped.
    """
    windows = []
    year = HISTORY_START.year
    while date(year, 1, 1) <= end:
        start, stop = date(year, 1, 1), min(date(year + WINDOW_YEARS - 1, 12, 31), end)
        if resume_after is not None:
            start = max(start, resume_after + timedelta(days=1))
        if start <= stop:
            windows.append((start, stop))
        year += WINDOW_YEARS
    return windows


class CandleData(StrictModel):
    candles: list[list[Any]]


class CandleEnvelope(StrictModel):
    status: Literal["success"]
    data: CandleData


class RawCandle(StrictModel):
    timestamp: str = Field(pattern=_MIDNIGHT_IST)
    open: Decimal = Field(ge=0)
    high: Decimal = Field(ge=0)
    low: Decimal = Field(ge=0)
    close: Decimal = Field(ge=0)
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)


@dataclass(frozen=True)
class Candle:
    candle_index: int  # 0-based position in the response
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int


@dataclass(frozen=True)
class CandleIssue:
    candle_index: int
    raw_candle: str
    reason: UpstoxQuarantineReason
    detail: str
    trade_date: date | None = None


@dataclass(frozen=True)
class ParsedCandles:
    candles: tuple[Candle, ...]  # oldest first
    issues: tuple[CandleIssue, ...]


class CandlesRejected(Exception):
    """The whole response is unusable. Its raw bytes are kept; nothing else is stored."""

    def __init__(self, reason: UpstoxQuarantineReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _as_decimal(value: Any) -> Any:
    """JSON integers stand for whole prices; everything else is left for strict validation."""
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return value


def _raw(values: list[Any]) -> str:
    return json.dumps(values, default=str)


def parse_candles(data: bytes, *, last_complete: date) -> ParsedCandles:
    """Validate one response. `last_complete` is `last_complete_day` of its fetch time."""
    try:
        payload = json.loads(data.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandlesRejected(UpstoxQuarantineReason.SHAPE_CHANGED, f"not JSON: {exc}") from None
    try:
        envelope = CandleEnvelope.model_validate(payload)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise CandlesRejected(
            UpstoxQuarantineReason.SHAPE_CHANGED, f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
        ) from None

    by_date: dict[date, list[tuple[str, Candle]]] = {}
    issues: list[CandleIssue] = []
    for index, values in enumerate(envelope.data.candles):
        raw = _raw(values)
        if len(values) != CANDLE_FIELDS:
            issues.append(
                CandleIssue(index, raw, UpstoxQuarantineReason.MALFORMED_CANDLE,
                            f"{len(values)} fields, expected {CANDLE_FIELDS}")
            )  # fmt: skip
            continue
        ts, o, h, lo, c, vol, oi = values
        try:
            parsed = RawCandle(
                timestamp=ts, open=_as_decimal(o), high=_as_decimal(h), low=_as_decimal(lo),
                close=_as_decimal(c), volume=vol, open_interest=oi,
            )  # fmt: skip
        except ValidationError as exc:
            error = exc.errors()[0]
            issues.append(
                CandleIssue(index, raw, UpstoxQuarantineReason.MALFORMED_CANDLE,
                            f"{'.'.join(map(str, error['loc']))}: {error['msg']}")
            )  # fmt: skip
            continue
        try:
            trade_date = date.fromisoformat(parsed.timestamp[:10])
        except ValueError as exc:
            issues.append(CandleIssue(index, raw, UpstoxQuarantineReason.MALFORMED_CANDLE, f"timestamp: {exc}"))
            continue
        if trade_date > last_complete:
            issues.append(
                CandleIssue(index, raw, UpstoxQuarantineReason.MALFORMED_CANDLE,
                            f"dated after the last complete day {last_complete}", trade_date)
            )  # fmt: skip
            continue
        if not (
            parsed.high >= max(parsed.open, parsed.close, parsed.low)
            and parsed.low <= min(parsed.open, parsed.close)
        ):
            issues.append(
                CandleIssue(index, raw, UpstoxQuarantineReason.OHLC_INCONSISTENT,
                            f"O={parsed.open} H={parsed.high} L={parsed.low} C={parsed.close}", trade_date)
            )  # fmt: skip
            continue
        by_date.setdefault(trade_date, []).append(
            (raw, Candle(index, trade_date, parsed.open, parsed.high, parsed.low, parsed.close,
                         parsed.volume, parsed.open_interest))
        )  # fmt: skip

    # Two bars for one day: row order is undocumented, so neither can be preferred.
    candles: list[Candle] = []
    for trade_date, group in sorted(by_date.items()):
        if len(group) == 1:
            candles.append(group[0][1])
            continue
        positions = [candle.candle_index for _, candle in group]
        issues.extend(
            CandleIssue(candle.candle_index, raw, UpstoxQuarantineReason.DUPLICATE_DATE,
                        f"{trade_date} appears at candles {positions}", trade_date)
            for raw, candle in group
        )  # fmt: skip
    return ParsedCandles(tuple(candles), tuple(sorted(issues, key=lambda i: i.candle_index)))


def require_isin(url: str) -> str:
    """The ISIN a response is for, from its request URL; rejects the response otherwise."""
    isin = isin_from_url(url)
    if isin is None or not is_valid_isin(isin):
        raise CandlesRejected(
            UpstoxQuarantineReason.UNKNOWN_INSTRUMENT, f"no valid {EXCHANGE_SEGMENT} ISIN in {url}"
        )
    return isin
