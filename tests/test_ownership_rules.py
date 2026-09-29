"""Ownership rules: false-signal filters, net aggregation, threshold detection, critical alerts.

Pure, property-tested. Input is frozen dataclasses mirroring the DB models.
Output carries no verdicts (no buy/sell/hold/order): inflow/outflow only.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.ownership_rules import (
    RULE_VERSION,
    InsiderTradeInput,
    StakeDisclosureInput,
    BulkBlockDealInput,
    OwnershipSignal,
    filter_false_signals_insider_trades,
    filter_false_signals_stake_disclosures,
    filter_false_signals_bulk_block_deals,
    aggregate_net_flows_by_category,
    detect_critical_alerts,
)

D = Decimal


class TestInputDataclasses:
    """Frozen dataclasses that mirror DB models, with no I/O."""

    def test_insider_trade_input_is_frozen(self):
        """InsiderTradeInput instances are immutable."""
        trade = InsiderTradeInput(
            isin="INE848E01016",
            person_name="Mukesh Ambani",
            person_category="Promoter",
            acquisition_mode="open_market",
            trade_date=date(2026, 1, 15),
            quantity=D("1000"),
            price_per_share=D("2500.50"),
            post_trade_holding_pct=D("75.5"),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            trade.quantity = D("2000")

    def test_stake_disclosure_input_is_frozen(self):
        """StakeDisclosureInput instances are immutable."""
        disclosure = StakeDisclosureInput(
            isin="INE848E01016",
            acquirer_name="FII Fund Alpha",
            disclosure_type="sast_5pct",
            disclosure_date=date(2026, 1, 20),
            shares_acquired=None,
            post_acquisition_pct=D("5.2"),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            disclosure.acquirer_name = "Other Fund"

    def test_bulk_block_deal_input_is_frozen(self):
        """BulkBlockDealInput instances are immutable."""
        deal = BulkBlockDealInput(
            isin="INE848E01016",
            deal_type="bulk",
            deal_date=date(2026, 1, 25),
            client_name="Goldman Sachs",
            quantity=D("500000"),
            price_per_share=D("2450.00"),
            deal_side="inflow",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            deal.quantity = D("600000")


class TestFalseSignalFiltering:
    """Exclude known false positives per CLAUDE.md."""

    def test_insider_trade_esop_is_filtered(self):
        """ESOP/gift trades are excluded from signal calculation."""
        esop_trade = InsiderTradeInput(
            isin="INE848E01016",
            person_name="Employee X",
            person_category="Designated Employee",
            acquisition_mode="esos_esop",
            trade_date=date(2026, 1, 15),
            quantity=D("500"),
            price_per_share=D("2500.00"),
            post_trade_holding_pct=D("0.5"),
        )
        filtered = filter_false_signals_insider_trades([esop_trade])
        assert len(filtered) == 0, "ESOP trades should be filtered out"

    def test_insider_trade_gift_is_filtered(self):
        """Gift/inheritance trades are excluded."""
        gift_trade = InsiderTradeInput(
            isin="INE848E01016",
            person_name="Family Member",
            person_category="Relative of Promoter",
            acquisition_mode="gift",
            trade_date=date(2026, 1, 15),
            quantity=D("1000"),
            price_per_share=D("0"),  # Gifts have zero price
            post_trade_holding_pct=D("5.0"),
        )
        filtered = filter_false_signals_insider_trades([gift_trade])
        assert len(filtered) == 0, "Gift trades should be filtered out"

    def test_insider_trade_off_market_inter_se_is_filtered(self):
        """Off-market transfers between promoters are excluded."""
        inter_se = InsiderTradeInput(
            isin="INE848E01016",
            person_name="Promoter A",
            person_category="Promoter",
            acquisition_mode="off_market",
            trade_date=date(2026, 1, 15),
            quantity=D("10000"),
            price_per_share=D("2500.00"),
            post_trade_holding_pct=D("50.0"),
        )
        filtered = filter_false_signals_insider_trades([inter_se])
        assert len(filtered) == 0, "Off-market inter-promoter trades should be filtered"

    def test_insider_trade_open_market_is_kept(self):
        """Open-market trades are kept (signal-eligible)."""
        om_trade = InsiderTradeInput(
            isin="INE848E01016",
            person_name="Promoter A",
            person_category="Promoter",
            acquisition_mode="open_market",
            trade_date=date(2026, 1, 15),
            quantity=D("10000"),
            price_per_share=D("2500.00"),
            post_trade_holding_pct=D("75.0"),
        )
        filtered = filter_false_signals_insider_trades([om_trade])
        assert len(filtered) == 1, "Open-market trades should be kept"
        assert filtered[0] == om_trade

    def test_stake_disclosure_reclassification_is_filtered(self):
        """Promoter reclassification is excluded."""
        reclassification = StakeDisclosureInput(
            isin="INE848E01016",
            acquirer_name="Promoter Reclassed",
            disclosure_type="reclassification",
            disclosure_date=date(2026, 1, 20),
            shares_acquired=None,
            post_acquisition_pct=D("40.0"),
        )
        filtered = filter_false_signals_stake_disclosures([reclassification])
        assert len(filtered) == 0, "Reclassification should be filtered"

    def test_stake_disclosure_sast_5pct_is_kept(self):
        """SAST crossing is kept (signal-eligible)."""
        sast = StakeDisclosureInput(
            isin="INE848E01016",
            acquirer_name="FII Fund",
            disclosure_type="sast_5pct",
            disclosure_date=date(2026, 1, 20),
            shares_acquired=None,
            post_acquisition_pct=D("5.5"),
        )
        filtered = filter_false_signals_stake_disclosures([sast])
        assert len(filtered) == 1, "SAST 5% crossing should be kept"

    def test_bulk_deal_all_are_kept(self):
        """Bulk/block deals are kept (no false-signal filter)."""
        deals = [
            BulkBlockDealInput(
                isin="INE848E01016",
                deal_type="bulk",
                deal_date=date(2026, 1, 25),
                client_name="Goldman Sachs",
                quantity=D("500000"),
                price_per_share=D("2450.00"),
                deal_side="inflow",
            ),
            BulkBlockDealInput(
                isin="INE848E01016",
                deal_type="block",
                deal_date=date(2026, 1, 26),
                client_name="Goldman Sachs",
                quantity=D("200000"),
                price_per_share=D("2455.00"),
                deal_side="outflow",
            ),
        ]
        filtered = filter_false_signals_bulk_block_deals(deals)
        assert len(filtered) == 2, "No bulk/block deals should be filtered"


class TestNetAggregation:
    """Aggregate and net by category with Decimal precision."""

    def test_aggregate_insider_trades_by_person_category(self):
        """Sum quantities and compute net inflow/outflow by person category."""
        trades = [
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter A",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 15),
                quantity=D("5000"),
                price_per_share=D("2500.00"),
                post_trade_holding_pct=D("75.0"),
            ),
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter B",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 16),
                quantity=D("3000"),  # Negative for sale
                price_per_share=D("2510.00"),
                post_trade_holding_pct=D("40.0"),
            ),
        ]
        # After filtering false signals, aggregate net flow
        filtered = filter_false_signals_insider_trades(trades)
        agg = aggregate_net_flows_by_category(filtered)
        assert "insider_trades" in agg
        assert agg["insider_trades"]["Promoter"]["net_quantity"] == D("8000")
        assert agg["insider_trades"]["Promoter"]["direction"] in ["inflow", "outflow"]

    def test_aggregate_mixed_inflows_outflows(self):
        """Separate inflow (buy) and outflow (sell) volumes."""
        trades = [
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter A",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 15),
                quantity=D("10000"),  # Buy
                price_per_share=D("2500.00"),
                post_trade_holding_pct=D("75.0"),
            ),
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Insider",
                person_category="Key Managerial Personnel",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 16),
                quantity=D("-5000"),  # Sell
                price_per_share=D("2510.00"),
                post_trade_holding_pct=D("2.0"),
            ),
        ]
        filtered = filter_false_signals_insider_trades(trades)
        agg = aggregate_net_flows_by_category(filtered)
        # Promoter: net 10000 buy = inflow
        # KMP: net 5000 sell = outflow
        assert agg["insider_trades"]["Promoter"]["direction"] == "inflow"
        assert agg["insider_trades"]["Key Managerial Personnel"]["direction"] == "outflow"


class TestCriticalAlerts:
    """Detect critical-severity conditions per CLAUDE.md."""

    def test_pledge_invoked_is_critical(self):
        """Pledge invoked raises CRITICAL alert."""
        disclosures = [
            StakeDisclosureInput(
                isin="INE848E01016",
                acquirer_name="Promoter",
                disclosure_type="pledge_invoked",
                disclosure_date=date(2026, 1, 20),
                shares_acquired=D("100000"),
                post_acquisition_pct=D("25.0"),
            ),
        ]
        alerts = detect_critical_alerts(disclosures=disclosures, isin="INE848E01016")
        assert len(alerts) > 0
        assert any(a.severity == "critical" for a in alerts)
        assert any("pledge invoked" in a.detail.lower() for a in alerts)

    def test_promoter_sale_above_threshold_is_critical(self):
        """Promoter open-market sale above X% of their stake is critical."""
        # Assume 60% promoter stake, selling 15% of it = critical
        trades = [
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter Group",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 15),
                quantity=D("-15000000"),  # Large sale
                price_per_share=D("2500.00"),
                post_trade_holding_pct=D("45.0"),  # Was 60%, now 45% = 15% sale
            ),
        ]
        filtered = filter_false_signals_insider_trades(trades)
        alerts = detect_critical_alerts(filtered, isin="INE848E01016")
        assert len(alerts) > 0
        assert any(a.severity == "critical" for a in alerts)


class TestRuleVersion:
    """RULE_VERSION constant is set."""

    def test_rule_version_is_defined(self):
        """RULE_VERSION = 'ownership_rules/1'."""
        assert RULE_VERSION == "ownership_rules/1"


class TestEdgeCases:
    """Boundary conditions and error handling."""

    def test_empty_input_lists_produce_empty_aggregation(self):
        """Empty trade lists aggregate to empty result."""
        agg = aggregate_net_flows_by_category([])
        assert agg == {} or all(len(v) == 0 for v in agg.values())

    def test_decimal_precision_is_preserved(self):
        """Decimal arithmetic does not lose precision."""
        trades = [
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 15),
                quantity=D("123.456789"),  # Fractional shares
                price_per_share=D("2500.123456"),
                post_trade_holding_pct=D("75.123456789"),
            ),
        ]
        filtered = filter_false_signals_insider_trades(trades)
        agg = aggregate_net_flows_by_category(filtered)
        # Exact Decimal precision should be maintained
        assert agg["insider_trades"]["Promoter"]["net_quantity"] == D("123.456789")

    def test_signal_direction_is_inflow_or_outflow_not_verdict(self):
        """Output uses inflow/outflow, never buy/sell/hold/order."""
        trades = [
            InsiderTradeInput(
                isin="INE848E01016",
                person_name="Promoter",
                person_category="Promoter",
                acquisition_mode="open_market",
                trade_date=date(2026, 1, 15),
                quantity=D("10000"),
                price_per_share=D("2500.00"),
                post_trade_holding_pct=D("75.0"),
            ),
        ]
        filtered = filter_false_signals_insider_trades(trades)
        agg = aggregate_net_flows_by_category(filtered)
        direction = agg["insider_trades"]["Promoter"]["direction"]
        assert direction in ["inflow", "outflow"]
        assert direction not in ["buy", "sell", "hold", "order"]
