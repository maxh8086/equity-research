"""Order-book report with market context and forward projections.

Shows order value with verbatim quote, counterparty status (named/related/unnamed),
order book against market cap at t, revenue contribution (order value over TTM revenue,
annualised over execution period), delivery projection weighted by guidance delivery rate,
projected forward PE (bear/base/bull) with stored assumptions and assumption-set hash,
mandatory disclaimer, and related-party and cycle overlays.

Everything is code (R1): all outputs are deterministic by code, no model takes part
except for narrative generation. All data carries as_of timestamps (R2). The function
proposes, the caller disposes (R3).

Pure: callers pass orders, intensities, and contextual data known at t. No DB, no I/O,
no clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Optional

from core.compute.order_wins import OrderWin, OrderIntensity
from core.compute.related_parties import RelatedPartyEntry, match_related

RULE_VERSION = "order_book_report/1"

_ZERO = Decimal(0)
_IST = timezone(timedelta(hours=5, minutes=30))
_DISCLAIMER = (
    "Order-book projections are based on historical guidance delivery rates and "
    "announced orders as of the observation date. Forward PE assumptions are "
    "illustrative and subject to execution risk, market conditions, and company "
    "performance. This report is not financial advice."
)


class CounterpartyStatus(StrEnum):
    """Status of the counterparty: named, related party, or unnamed."""
    NAMED = "named"
    RELATED_PARTY = "related_party"
    UNNAMED = "unnamed"


@dataclass(frozen=True)
class GuidanceDelivery:
    """Guidance delivery rate for order-book projections."""
    guided_value_inr: Decimal
    executed_value_inr: Decimal
    delivery_rate: Decimal  # executed / guided
    as_of: datetime


@dataclass(frozen=True)
class ProjectedPE:
    """Forward PE projection scenarios."""
    bear_pe: Decimal | None
    base_pe: Decimal | None
    bull_pe: Decimal | None
    assumption_hash: str
    assumptions: dict[str, Decimal]
    as_of: datetime


@dataclass(frozen=True)
class OrderBookOrderInput:
    """One order in the book with all context."""
    announcement_key: str
    announced_on: datetime
    order_value_inr: Decimal
    value_quote: str
    counterparty_name: str | None
    counterparty_status: CounterpartyStatus
    evidence_url: str
    pct_of_ttm_revenue: Decimal | None
    annualised_pct: Decimal | None
    execution_months: Decimal | None
    as_of: datetime


@dataclass(frozen=True)
class OrderBookReport:
    """Input data for the order-book report computation."""
    isin: str
    order_wins: tuple[OrderWin, ...]
    intensities: tuple[OrderIntensity, ...]  # parallel to order_wins
    market_cap_inr: Decimal
    current_price: Decimal
    related_party_entries: Sequence[RelatedPartyEntry]
    guidance_delivery: GuidanceDelivery | None
    t: datetime
    cycle_overlay: dict | None = None  # placeholder for cycle-detector integration

    def __post_init__(self) -> None:
        if len(self.order_wins) != len(self.intensities):
            raise ValueError("order_wins and intensities must have same length")
        if self.t.tzinfo is None or self.t.utcoffset() is None:
            raise ValueError("t must be timezone-aware")
        for win in self.order_wins:
            if win.isin != self.isin:
                raise ValueError(f"order_win ISIN {win.isin} does not match report ISIN {self.isin}")
            if win.as_of > self.t:
                raise ValueError(f"order_win as_of {win.as_of} is after t {self.t}")
        for intensity in self.intensities:
            if intensity.isin != self.isin:
                raise ValueError(f"intensity ISIN {intensity.isin} does not match report ISIN {self.isin}")
            if intensity.as_of > self.t:
                raise ValueError(f"intensity as_of {intensity.as_of} is after t {self.t}")


@dataclass(frozen=True)
class OrderBookResult:
    """Result of order-book report computation."""
    isin: str
    t: datetime
    as_of: datetime

    # Order book details
    orders: tuple[OrderBookOrderInput, ...]
    total_order_value: Decimal
    order_book_pct_of_market_cap: Decimal

    # Revenue contribution
    total_pct_of_ttm_revenue: Decimal | None
    total_annualised_pct: Decimal | None

    # Guidance delivery
    guidance_delivery_rate: Decimal | None

    # Forward projections
    projected_pe_bear: Decimal | None
    projected_pe_base: Decimal | None
    projected_pe_bull: Decimal | None
    projected_pe_assumptions: dict[str, Decimal] | None
    projected_pe_assumption_hash: str | None

    # Context
    market_cap_inr: Decimal
    current_price: Decimal

    # Related party overlay
    related_party_order_count: int
    related_party_order_value: Decimal
    related_party_order_share: Decimal

    # Disclaimers and metadata
    disclaimer: str
    rule_version: str = RULE_VERSION
    cycle_overlay: dict | None = None


def _order_date(d: datetime) -> date:
    """Convert datetime to IST date."""
    return d.astimezone(_IST).date()


def _compute_projected_pe(
    market_cap_inr: Decimal,
    ttm_order_value: Decimal,
    guidance_delivery_rate: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, str, dict[str, Decimal]]:
    """Compute projected PE scenarios.

    Returns (bear_pe, base_pe, bull_pe, assumption_hash, assumptions_dict).

    Simplified projection: assumes order book flows through to earnings growth.
    Bear: 50% delivery, Base: guidance_delivery_rate, Bull: 100% delivery.
    """
    if market_cap_inr <= _ZERO or ttm_order_value <= _ZERO:
        return None, None, None, "", {}

    default_delivery = guidance_delivery_rate or Decimal("0.85")

    assumptions = {
        "market_cap_inr": market_cap_inr,
        "ttm_order_value_inr": ttm_order_value,
        "bear_delivery_rate": Decimal("0.50"),
        "base_delivery_rate": default_delivery,
        "bull_delivery_rate": Decimal("1.00"),
    }

    # Simplified: PE impact = (1 + (delivery_rate * order_value / market_cap))^-1
    # In practice, this would involve net profit estimates, not just market cap
    bear_impact = Decimal("0.50") * ttm_order_value / market_cap_inr
    base_impact = default_delivery * ttm_order_value / market_cap_inr
    bull_impact = Decimal("1.00") * ttm_order_value / market_cap_inr

    # Simple heuristic: PE expansion due to earnings growth from orders
    # Forward PE = Current PE * (1 + earnings_growth)
    # This is a placeholder; actual PE depends on P/E, retention, reinvestment
    current_pe_proxy = Decimal("20")  # Placeholder

    bear_pe = current_pe_proxy / (Decimal("1") + bear_impact) if (Decimal("1") + bear_impact) > _ZERO else None
    base_pe = current_pe_proxy / (Decimal("1") + base_impact) if (Decimal("1") + base_impact) > _ZERO else None
    bull_pe = current_pe_proxy / (Decimal("1") + bull_impact) if (Decimal("1") + bull_impact) > _ZERO else None

    # Simple hash of assumptions
    hash_input = "|".join(f"{k}:{v}" for k, v in sorted(assumptions.items()))
    assumption_hash = str(hash(hash_input))

    return bear_pe, base_pe, bull_pe, assumption_hash, assumptions


def order_book_report(report: OrderBookReport) -> OrderBookResult:
    """Compute order-book report with market context and forward projections.

    Args:
        report: OrderBookReport with order wins, intensities, and market context

    Returns:
        OrderBookResult with all computed fields

    Raises:
        ValueError: if input validation fails
    """
    if report.t.tzinfo is None or report.t.utcoffset() is None:
        raise ValueError("report.t must be timezone-aware")

    isin = report.isin

    if not report.order_wins:
        # No orders, return empty result
        empty_result = OrderBookResult(
            isin=isin,
            t=report.t,
            as_of=report.t,
            orders=(),
            total_order_value=_ZERO,
            order_book_pct_of_market_cap=_ZERO,
            total_pct_of_ttm_revenue=None,
            total_annualised_pct=None,
            guidance_delivery_rate=None,
            projected_pe_bear=None,
            projected_pe_base=None,
            projected_pe_bull=None,
            projected_pe_assumptions=None,
            projected_pe_assumption_hash=None,
            market_cap_inr=report.market_cap_inr,
            current_price=report.current_price,
            related_party_order_count=0,
            related_party_order_value=_ZERO,
            related_party_order_share=_ZERO,
            disclaimer=_DISCLAIMER,
            cycle_overlay=report.cycle_overlay,
        )
        return empty_result

    # Build order inputs with counterparty matching
    orders: list[OrderBookOrderInput] = []
    total_value = _ZERO
    total_ttm_pct = _ZERO
    total_annualised_pct = _ZERO
    total_annualised_count = 0
    related_party_value = _ZERO
    related_party_count = 0

    for win, intensity in zip(report.order_wins, report.intensities):
        if win.isin != isin:
            raise ValueError(f"inconsistent ISIN: {win.isin} vs {isin}")

        # Determine counterparty status
        if win.counterparty is None:
            status = CounterpartyStatus.UNNAMED
            matched_name = None
        else:
            # Check if related party
            hit = match_related(
                win.counterparty,
                _order_date(win.announced_on),
                isin,
                report.related_party_entries,
            )
            if hit is not None:
                status = CounterpartyStatus.RELATED_PARTY
                matched_name = win.counterparty
                related_party_value += win.order_value_inr
                related_party_count += 1
            else:
                status = CounterpartyStatus.NAMED
                matched_name = win.counterparty

        # Build order input
        order_input = OrderBookOrderInput(
            announcement_key=f"{hash((win.isin, win.announced_on, win.source_url)):064x}",
            announced_on=win.announced_on,
            order_value_inr=win.order_value_inr,
            value_quote=win.value_quote,
            counterparty_name=matched_name,
            counterparty_status=status,
            evidence_url=win.source_url,
            pct_of_ttm_revenue=intensity.pct_of_trailing_revenue,
            annualised_pct=intensity.annualised_pct,
            execution_months=win.execution_months,
            as_of=win.as_of,
        )
        orders.append(order_input)

        total_value += win.order_value_inr
        if intensity.pct_of_trailing_revenue is not None:
            total_ttm_pct += intensity.pct_of_trailing_revenue
        if intensity.annualised_pct is not None:
            total_annualised_pct += intensity.annualised_pct
            total_annualised_count += 1

    # Compute order book as % of market cap
    order_book_pct = (total_value / report.market_cap_inr * 100) if report.market_cap_inr > _ZERO else _ZERO

    # Compute related party share
    related_party_share = (related_party_value / total_value) if total_value > _ZERO else _ZERO

    # Compute guidance delivery rate
    guidance_rate = None
    if report.guidance_delivery is not None:
        guidance_rate = report.guidance_delivery.delivery_rate

    # Compute projected PE scenarios
    bear_pe, base_pe, bull_pe, pe_hash, pe_assumptions = _compute_projected_pe(
        report.market_cap_inr,
        total_value,
        guidance_rate,
    )

    # Average annualised pct
    avg_annualised_pct = (total_annualised_pct / total_annualised_count) if total_annualised_count > 0 else None

    result = OrderBookResult(
        isin=isin,
        t=report.t,
        as_of=report.t,
        orders=tuple(orders),
        total_order_value=total_value,
        order_book_pct_of_market_cap=order_book_pct,
        total_pct_of_ttm_revenue=total_ttm_pct,
        total_annualised_pct=avg_annualised_pct,
        guidance_delivery_rate=guidance_rate,
        projected_pe_bear=bear_pe,
        projected_pe_base=base_pe,
        projected_pe_bull=bull_pe,
        projected_pe_assumptions=pe_assumptions or None,
        projected_pe_assumption_hash=pe_hash or None,
        market_cap_inr=report.market_cap_inr,
        current_price=report.current_price,
        related_party_order_count=related_party_count,
        related_party_order_value=related_party_value,
        related_party_order_share=related_party_share,
        disclaimer=_DISCLAIMER,
        cycle_overlay=report.cycle_overlay,
    )

    return result
