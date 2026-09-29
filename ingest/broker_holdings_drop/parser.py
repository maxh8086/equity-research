"""Parse a JSON dump of Zerodha Kite `get_holdings` output into positions keyed by ISIN.

Only ISIN, exchange, quantity and the two prices are read; every other field
of a record (day change, pnl, collateral, mtf, ...) is ignored, and so is the
trading symbol: a series suffix such as "-BE" must not make a different
security, and the same company appears on NSE or BSE under one ISIN.

Money is parsed from the JSON number's own text into `Decimal`, never through
float. Quantities are integers. A record that cannot be trusted is returned as
a quarantined row with its reason; a file that is not a holdings list at all
raises `HoldingsRejected`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from core.db.models import BrokerHoldingQuarantineReason as Reason

ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
EXCHANGES = frozenset({"NSE", "BSE"})
MAX_QUANTITY = 2**63 - 1  # BIGINT
PRICE_PLACES = Decimal("0.000001")  # NUMERIC(28, 6): never round silently
MAX_PRICE = Decimal(10) ** 22


class HoldingsRejected(Exception):
    """The file is not a list of holdings records; nothing in it is used."""


@dataclass(frozen=True)
class HoldingRow:
    row_number: int
    isin: str
    exchange: str
    quantity: int
    average_price: Decimal
    last_price: Decimal


@dataclass(frozen=True)
class QuarantinedRow:
    row_number: int
    isin: str | None
    reason: Reason
    detail: str


@dataclass(frozen=True)
class ParsedHoldings:
    rows: list[HoldingRow]
    quarantined: list[QuarantinedRow]


class _Bad(Exception):
    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _load(data: bytes) -> list[object]:
    try:
        document = json.loads(
            data.decode("utf-8-sig"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=Decimal,  # NaN and Infinity become non-finite Decimals, caught per field
        )
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise HoldingsRejected(f"not JSON: {exc}") from exc
    if isinstance(document, dict) and isinstance(document.get("holdings"), list):
        document = document["holdings"]
    if not isinstance(document, list):
        raise HoldingsRejected("expected a list of holdings records, or an object with a 'holdings' list")
    if not document:
        raise HoldingsRejected("the list of holdings is empty")
    if not all(isinstance(record, dict) for record in document):
        raise HoldingsRejected("every holdings record must be a JSON object")
    return document


def _isin(record: dict) -> str:
    raw = record.get("isin")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _Bad(Reason.MISSING_ISIN, "isin is absent or empty")
    if not isinstance(raw, str) or not ISIN_RE.match(raw.strip()):
        raise _Bad(Reason.INVALID_ISIN, f"isin {raw!r} is not an ISIN")
    return raw.strip()


def _quantity(record: dict) -> int:
    raw = record.get("quantity")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"quantity {raw!r} is not an integer")
    if raw < 0:
        raise _Bad(Reason.NEGATIVE_QUANTITY, f"quantity {raw} is negative")
    if raw > MAX_QUANTITY:
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"quantity {raw} is out of range")
    return raw


def _price(record: dict, field: str) -> Decimal:
    raw = record.get(field)
    if isinstance(raw, bool) or not isinstance(raw, int | Decimal):
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"{field} {raw!r} is not a number")
    value = Decimal(raw)
    if not value.is_finite():
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"{field} {raw!r} is not finite")
    if value < 0:
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"{field} {raw} is negative")
    if value >= MAX_PRICE:
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"{field} {raw} is out of range")
    if value != value.quantize(PRICE_PLACES):
        raise _Bad(Reason.UNPARSEABLE_NUMBER, f"{field} {raw} has more than 6 decimal places")
    return value


def parse_holdings(data: bytes) -> ParsedHoldings:
    """Split a holdings dump into storable positions and quarantined records."""
    rows: list[HoldingRow] = []
    quarantined: list[QuarantinedRow] = []
    seen: set[tuple[str, str]] = set()
    for number, record in enumerate(_load(data), start=1):
        assert isinstance(record, dict)
        raw_isin = record.get("isin")
        shown = None if raw_isin is None else str(raw_isin)
        try:
            isin = _isin(record)
            exchange = record.get("exchange")
            if exchange not in EXCHANGES:
                raise _Bad(Reason.UNKNOWN_EXCHANGE, f"exchange {exchange!r} is not NSE or BSE")
            quantity = _quantity(record)
            average_price = _price(record, "average_price")
            last_price = _price(record, "last_price")
            if (isin, exchange) in seen:
                raise _Bad(Reason.DUPLICATE_ROW, f"{isin} on {exchange} already appears in this file")
        except _Bad as bad:
            quarantined.append(QuarantinedRow(number, shown if shown else None, bad.reason, bad.detail))
            continue
        seen.add((isin, exchange))
        rows.append(HoldingRow(number, isin, exchange, quantity, average_price, last_price))
    return ParsedHoldings(rows, quarantined)
