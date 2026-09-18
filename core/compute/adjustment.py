"""Price adjustment factors for corporate actions, and applying them to a series.

Pure: callers pass the actions and prices in. A factor multiplies every price
dated strictly before the ex-date, so a series read at `t` is continuous
across splits, bonuses, rights and demergers that took effect by `t`.

Convention (research decision, 3e): the price series adjusts for capital
actions only (split, consolidation, bonus, rights, demerger), like NSE's own
charts. Dividends are not applied to prices; `dividend_factor` exists for a
separate total-return series.

Which actions are known and effective at `t` is decided by the caller
(core.db.pit.price_adjustments_as_of), never here: this module has no clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

RULE_VERSION = "adjustment/1"

ONE = Decimal(1)

# Products of a few factors are kept exact (no rounding between steps), so the
# order in which actions are applied can never change an adjusted price.
PRODUCT_PRECISION = 80


@dataclass(frozen=True)
class PriceFactor:
    """Multiply prices dated before `ex_date` by `factor` (0 < factor)."""

    ex_date: date
    factor: Decimal

    def __post_init__(self) -> None:
        if not self.factor > 0:
            raise ValueError(f"factor must be positive, got {self.factor}")


def _positive(**values: Decimal | int) -> None:
    for name, value in values.items():
        if not value > 0:
            raise ValueError(f"{name} must be positive, got {value}")


def split_factor(face_value_from: Decimal, face_value_to: Decimal) -> Decimal:
    """Sub-division (10 -> 2 gives 0.2) or consolidation (1 -> 10 gives 10)."""
    _positive(face_value_from=face_value_from, face_value_to=face_value_to)
    return Decimal(face_value_to) / Decimal(face_value_from)


def bonus_factor(shares_new: int, shares_held: int) -> Decimal:
    """`shares_new` bonus shares for every `shares_held` (1:1 gives 0.5)."""
    _positive(shares_new=shares_new, shares_held=shares_held)
    return Decimal(shares_held) / Decimal(shares_held + shares_new)


def rights_factor(shares_new: int, shares_held: int, issue_price: Decimal, cum_close: Decimal) -> Decimal:
    """Theoretical ex-rights price over the last cum-rights close.

    TERP = (held * cum_close + new * issue_price) / (held + new). A rights
    issue priced at or above the market dilutes no one's value: factor 1.
    """
    _positive(shares_new=shares_new, shares_held=shares_held, cum_close=cum_close)
    if issue_price < 0:
        raise ValueError(f"issue_price must not be negative, got {issue_price}")
    if issue_price >= cum_close:
        return ONE
    terp = (shares_held * cum_close + shares_new * issue_price) / Decimal(shares_held + shares_new)
    return terp / cum_close


def demerger_factor(retained_fraction: Decimal) -> Decimal:
    """The share of the pre-demerger value the parent keeps (the company's cost apportionment)."""
    if not ONE > retained_fraction > 0:
        raise ValueError(f"retained_fraction must be in (0, 1), got {retained_fraction}")
    return Decimal(retained_fraction)


def dividend_factor(dividend_per_share: Decimal, cum_close: Decimal) -> Decimal:
    """(cum_close - dividend) / cum_close: for a total-return series only, never the price series."""
    _positive(cum_close=cum_close)
    if not cum_close > dividend_per_share >= 0:
        raise ValueError(f"dividend {dividend_per_share} must be in [0, cum_close {cum_close})")
    return (cum_close - dividend_per_share) / cum_close


def cumulative_factor(factors: Iterable[PriceFactor], on: date) -> Decimal:
    """The product of every factor whose ex-date is after `on`: what a price on `on` is multiplied by."""
    product = ONE
    with localcontext(prec=PRODUCT_PRECISION):
        for f in factors:
            if f.ex_date > on:
                product *= f.factor
    return product


def adjust_series(prices: Mapping[date, Decimal], factors: Iterable[PriceFactor]) -> dict[date, Decimal]:
    """Every price multiplied by the cumulative factor for its date. The input is not changed."""
    ordered = sorted(factors, key=lambda f: (f.ex_date, f.factor))
    out: dict[date, Decimal] = {}
    with localcontext(prec=PRODUCT_PRECISION):
        for day, price in prices.items():
            out[day] = price * cumulative_factor(ordered, day)
    return out
