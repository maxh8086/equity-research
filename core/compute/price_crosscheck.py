"""Compare a vendor's daily bars with the exchange's own record of the same days.

Pure: callers pass both series in. The exchange side (NSE bhavcopy) is the
book of record for what traded; the vendor side (Upstox) may be adjusted for
later corporate actions or simply wrong. A mismatch is a finding for a person
or a later rule, never a correction: neither series is edited.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

RULE_VERSION = "price_crosscheck/1"

# Research decision: one paisa. Both sources quote to the tick, so an honest
# pair of as-traded bars agrees exactly; this only absorbs a vendor's float
# rendering of the same number (e.g. 53.1 vs 53.10).
PRICE_TOLERANCE = Decimal("0.01")

PRICE_FIELDS = ("open", "high", "low", "close")


class MismatchKind(StrEnum):
    MISSING_AT_VENDOR = "missing_at_vendor"  # exchange traded it, vendor has no bar
    MISSING_AT_EXCHANGE = "missing_at_exchange"  # vendor bar with no exchange row
    PRICE_DIFFERS = "price_differs"
    VOLUME_DIFFERS = "volume_differs"


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Mismatch:
    trade_date: date
    kind: MismatchKind
    field: str | None = None
    vendor: Decimal | int | None = None
    exchange: Decimal | int | None = None


def crosscheck(vendor: Mapping[date, Bar], exchange: Mapping[date, Bar]) -> list[Mismatch]:
    """Every day on which the two series disagree, oldest first.

    Callers pass the same date window for both sides; a date outside what one
    side covers shows up as missing, so trim the window to the overlap first.
    """
    out: list[Mismatch] = []
    for day in sorted(set(vendor) | set(exchange)):
        v, x = vendor.get(day), exchange.get(day)
        if v is None:
            out.append(Mismatch(day, MismatchKind.MISSING_AT_VENDOR))
            continue
        if x is None:
            out.append(Mismatch(day, MismatchKind.MISSING_AT_EXCHANGE))
            continue
        for name in PRICE_FIELDS:
            vp, xp = getattr(v, name), getattr(x, name)
            if abs(vp - xp) > PRICE_TOLERANCE:
                out.append(Mismatch(day, MismatchKind.PRICE_DIFFERS, name, vp, xp))
        if v.volume != x.volume:
            out.append(Mismatch(day, MismatchKind.VOLUME_DIFFERS, "volume", v.volume, x.volume))
    return out
