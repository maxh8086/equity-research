"""Value and weight of each position in a set of broker holdings: facts only.

Pure Decimal arithmetic (R1). No threshold, no label, no judgement: a caller
that wants to compare a weight with a limit owns that limit. Rows of one ISIN
(the same company on NSE and on BSE) are one position.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class HoldingInput:
    isin: str
    quantity: int
    last_price: Decimal


@dataclass(frozen=True)
class HoldingWeightFact:
    isin: str
    quantity: int
    value: Decimal  # quantity x last price, summed over the ISIN's rows
    weight: Decimal | None  # value / total_value; None when the total is zero


@dataclass(frozen=True)
class HoldingWeightFacts:
    total_value: Decimal
    holdings: tuple[HoldingWeightFact, ...]  # ordered by ISIN


def _checked(row: HoldingInput) -> None:
    if isinstance(row.quantity, bool) or not isinstance(row.quantity, int) or row.quantity < 0:
        raise ValueError(f"{row.isin}: quantity must be a non-negative integer, got {row.quantity!r}")
    if not isinstance(row.last_price, Decimal) or not row.last_price.is_finite() or row.last_price < 0:
        raise ValueError(f"{row.isin}: last_price must be a finite non-negative Decimal, got {row.last_price!r}")


def holding_weight_facts(rows: Iterable[HoldingInput]) -> HoldingWeightFacts:
    """Each position's value and its weight of the total, ordered by ISIN."""
    quantity: dict[str, int] = {}
    value: dict[str, Decimal] = {}
    for row in rows:
        _checked(row)
        quantity[row.isin] = quantity.get(row.isin, 0) + row.quantity
        value[row.isin] = value.get(row.isin, Decimal(0)) + row.quantity * row.last_price
    total = sum(value.values(), Decimal(0))
    holdings = tuple(
        HoldingWeightFact(isin, quantity[isin], value[isin], value[isin] / total if total else None)
        for isin in sorted(value)
    )
    return HoldingWeightFacts(total, holdings)
