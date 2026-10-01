"""Tests for core.compute.order_book_report module.

The order-book report shows:
- Order value with verbatim quote
- Who gave the order (named counterparty, related-party status, or "unnamed")
- Order book against market cap at t
- Revenue contribution (order value over TTM revenue, annualised over execution period)
- Delivery projection weighted by guidance delivery rate and executed-versus-declared
- Projected forward PE (bear/base/bull) with stored assumptions and assumption-set hash
- Mandatory disclaimer
- Related-party and cycle overlay
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from core.compute.order_book_report import (
    OrderBookOrderInput,
    OrderBookReport,
    OrderBookResult,
    GuidanceDelivery,
    ProjectedPE,
    order_book_report,
    CounterpartyStatus,
)
from core.compute.order_wins import OrderWin, OrderIntensity
from core.compute.related_parties import RelatedPartyEntry


ISIN = "INE848E01016"
UTC = timezone.utc
T = datetime(2025, 6, 30, 12, tzinfo=UTC)


def _order_win(
    value_inr: Decimal,
    execution_months: Decimal | None = Decimal(12),
    counterparty: str | None = "Alpha Corp",
    announced_days_ago: int = 30,
) -> OrderWin:
    """Create a test OrderWin."""
    return OrderWin(
        isin=ISIN,
        announced_on=T - timedelta(days=announced_days_ago),
        as_of=T - timedelta(days=1),
        source_url="https://example.com/order/1",
        order_value_inr=value_inr,
        value_quote="Order value of Rs 100 Cr",
        counterparty=counterparty,
        execution_months=execution_months,
        execution_end=None,
        period_quote="Within 12 months",
        matched_phrase="received order for",
        missing=(),
    )


def _order_intensity(
    order_value_inr: Decimal = Decimal(10000),
    trailing_revenue_inr: Decimal | None = Decimal(100000),
    execution_months: Decimal | None = Decimal(12),
    pct_of_trailing_revenue: Decimal | None = None,
) -> OrderIntensity:
    """Create a test OrderIntensity."""
    if pct_of_trailing_revenue is None and trailing_revenue_inr is not None:
        pct_of_trailing_revenue = (order_value_inr / trailing_revenue_inr * 100)

    return OrderIntensity(
        isin=ISIN,
        order_value_inr=order_value_inr,
        trailing_revenue_inr=trailing_revenue_inr,
        trailing_period_end=date(2025, 3, 31),
        execution_months=execution_months,
        pct_of_trailing_revenue=pct_of_trailing_revenue,
        annualised_pct=pct_of_trailing_revenue / (execution_months / 12) if execution_months and pct_of_trailing_revenue else None,
        gaps=() if (trailing_revenue_inr and execution_months) else ("gaps",),
        as_of=T - timedelta(days=1),
    )


def test_rule_version():
    """Verify rule version is set."""
    from core.compute import order_book_report as mod
    assert hasattr(mod, 'RULE_VERSION')
    assert mod.RULE_VERSION.startswith("order_book_report/")


def test_basic_order_book_report():
    """Basic test: create a report with one order."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert result.isin == ISIN
    assert result.t == T
    assert len(result.orders) >= 1
    assert result.order_book_pct_of_market_cap is not None


def test_order_with_counterparty():
    """Order with known counterparty is included."""
    win = _order_win(Decimal(10000), counterparty="Alpha Corp")
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert len(result.orders) == 1
    order = result.orders[0]
    assert order.counterparty_name == "Alpha Corp"
    assert order.counterparty_status == "named"


def test_order_without_counterparty():
    """Order without counterparty marked as unnamed."""
    win = _order_win(Decimal(10000), counterparty=None)
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    order = result.orders[0]
    assert order.counterparty_status == "unnamed"


def test_related_party_overlay():
    """Related-party entries are matched and flagged."""
    related_entry = RelatedPartyEntry(
        isin=ISIN,
        names=("Alpha Corp", "Alpha Limited"),
        valid_from=date(2024, 1, 1),
        valid_to=None,
        source_note="AR 2024",
    )

    win = _order_win(Decimal(10000), counterparty="Alpha Limited")
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(related_entry,),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    order = result.orders[0]
    assert order.counterparty_status == "related_party"


def test_revenue_contribution():
    """Revenue contribution is calculated correctly."""
    # 10% of TTM revenue, 12-month execution = 10% annualised
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(
        order_value_inr=Decimal(10000),
        trailing_revenue_inr=Decimal(100000),
        execution_months=Decimal(12),
    )

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)
    order = result.orders[0]

    assert order.pct_of_ttm_revenue == Decimal(10)
    assert order.annualised_pct is not None


def test_order_book_against_market_cap():
    """Order book value is compared to market cap."""
    win1 = _order_win(Decimal(50000))
    win2 = _order_win(Decimal(30000), announced_days_ago=60)

    intensity1 = _order_intensity(Decimal(50000))
    intensity2 = _order_intensity(Decimal(30000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win1, win2),
        intensities=(intensity1, intensity2),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    total_order_value = Decimal(50000) + Decimal(30000)
    expected_pct = (total_order_value / Decimal(1000000)) * 100
    assert result.order_book_pct_of_market_cap == expected_pct


def test_multiple_orders_aggregation():
    """Multiple orders are aggregated correctly."""
    orders = [
        _order_win(Decimal(10000), announced_days_ago=30),
        _order_win(Decimal(20000), announced_days_ago=60),
        _order_win(Decimal(15000), announced_days_ago=90),
    ]
    intensities = [_order_intensity(o.order_value_inr) for o in orders]

    report = OrderBookReport(
        isin=ISIN,
        order_wins=tuple(orders),
        intensities=tuple(intensities),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert len(result.orders) == 3
    assert result.total_order_value == Decimal(45000)


def test_guidance_delivery_projection():
    """Guidance delivery is applied to projections."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    delivery = GuidanceDelivery(
        guided_value_inr=Decimal(100000),
        executed_value_inr=Decimal(90000),
        delivery_rate=Decimal("0.9"),
        as_of=T,
    )

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=delivery,
        t=T,
    )

    result = order_book_report(report)

    assert result.guidance_delivery_rate == Decimal("0.9")


def test_projected_pe():
    """Projected PE scenarios are included in result."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    # Result should include bear/base/bull scenarios
    assert result.projected_pe_bear is not None
    assert result.projected_pe_base is not None
    assert result.projected_pe_bull is not None


def test_disclaimer_always_present():
    """Mandatory disclaimer is always included."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert result.disclaimer is not None
    assert len(result.disclaimer) > 0


def test_empty_order_book():
    """Empty order book produces valid result."""
    report = OrderBookReport(
        isin=ISIN,
        order_wins=(),
        intensities=(),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert result.isin == ISIN
    assert len(result.orders) == 0
    assert result.total_order_value == Decimal(0)


def test_timezone_aware_timestamps():
    """All timestamps must be timezone-aware."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert result.t.tzinfo is not None
    for order in result.orders:
        assert order.announced_on.tzinfo is not None


def test_order_with_missing_fields():
    """Orders with missing fields are flagged in the result."""
    win = _order_win(
        Decimal(10000),
        counterparty=None,  # missing counterparty
        execution_months=None,  # missing execution period
    )
    intensity = _order_intensity(
        order_value_inr=Decimal(10000),
        execution_months=None,  # no execution period
    )

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)
    order = result.orders[0]

    assert order.counterparty_status == "unnamed"
    assert order.annualised_pct is None  # can't annualise without execution_months


def test_value_quote_preserved():
    """Order value quote is preserved for verification."""
    win = _order_win(Decimal(10000))
    win = OrderWin(
        isin=win.isin,
        announced_on=win.announced_on,
        as_of=win.as_of,
        source_url=win.source_url,
        order_value_inr=win.order_value_inr,
        value_quote="Rs 1 Crore",
        counterparty=win.counterparty,
        execution_months=win.execution_months,
        execution_end=win.execution_end,
        period_quote=win.period_quote,
        matched_phrase=win.matched_phrase,
        missing=win.missing,
    )
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)
    order = result.orders[0]

    assert order.value_quote == "Rs 1 Crore"


def test_as_of_timestamp_preserved():
    """All as_of timestamps are preserved for audit trail."""
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    assert result.as_of == T
    assert result.orders[0].as_of is not None


def test_cycle_overlay_integration_placeholder():
    """Cycle overlay from cycle-detector worker will integrate here."""
    # Placeholder for cycle overlay integration when cycle-detector is ready
    # The result should include cycle_overlay field when available
    win = _order_win(Decimal(10000))
    intensity = _order_intensity(Decimal(10000))

    report = OrderBookReport(
        isin=ISIN,
        order_wins=(win,),
        intensities=(intensity,),
        market_cap_inr=Decimal(1000000),
        current_price=Decimal(500),
        related_party_entries=(),
        guidance_delivery=None,
        t=T,
    )

    result = order_book_report(report)

    # For now, cycle_overlay can be None, but the field should exist
    assert hasattr(result, 'cycle_overlay')
