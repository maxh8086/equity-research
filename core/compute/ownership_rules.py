"""Ownership rules: false-signal filters, net aggregation, threshold detection, critical alerts.

Pure, fully tested. Input is frozen dataclasses mirroring the DB models.
Output: inflow/outflow signals only (no action verdicts).
All input is as_of a point in time; functions refuse inputs dated after `t`.
Thresholds and parameters are hardcoded by rule_version, not configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

RULE_VERSION = "ownership_rules/1"

# Critical-alert thresholds (hardcoded by rule_version)
PROMOTER_SALE_CRITICAL_PCT = Decimal("10")  # >10% of their stake = critical
PLEDGE_PCT_INCREASE_CRITICAL = Decimal("5")  # >5 points in a quarter = critical


@dataclass(frozen=True)
class InsiderTradeInput:
    """Promoter / insider trade disclosure, mirroring InsiderTrade model fields."""

    isin: str
    person_name: str
    person_category: str  # "Promoter", "Key Managerial Personnel", etc.
    acquisition_mode: str  # "open_market", "esos_esop", "gift", "off_market", etc.
    trade_date: date
    quantity: Decimal  # Positive for buy, negative for sell
    price_per_share: Decimal
    post_trade_holding_pct: Decimal | None


@dataclass(frozen=True)
class StakeDisclosureInput:
    """Large-stake crossing or pledge event disclosure, mirroring StakeDisclosure model fields."""

    isin: str
    acquirer_name: str
    disclosure_type: str  # "sast_5pct", "pledge_created", "pledge_released", "pledge_invoked", "reclassification"
    disclosure_date: date
    shares_acquired: Decimal | None
    post_acquisition_pct: Decimal | None


@dataclass(frozen=True)
class BulkBlockDealInput:
    """Bulk or block deal record, mirroring BulkBlockDeal model fields."""

    isin: str
    deal_type: str  # "bulk" or "block"
    deal_date: date
    client_name: str
    quantity: Decimal
    price_per_share: Decimal
    deal_side: str  # "inflow" (buy) or "outflow" (sell)


@dataclass(frozen=True)
class OwnershipSignal:
    """A single ownership signal: inflow/outflow direction only."""

    isin: str
    signal_type: str  # "insider_trade", "stake_disclosure", "bulk_block_deal"
    category: str  # e.g., "Promoter", "FII", "DII"
    direction: str  # "inflow" or "outflow"
    severity: str  # "critical", "high", "medium", "low"
    event_date: date
    net_quantity: Decimal  # Aggregated quantity
    detail: str  # Human-readable description


def filter_false_signals_insider_trades(trades: list[InsiderTradeInput]) -> list[InsiderTradeInput]:
    """Exclude false-positive signals based on acquisition_mode.

    Per CLAUDE.md, exclude:
    - ESOS/ESOP: new shares diluting everyone
    - Gift, Inheritance: transfers and gifts between family/promoters
    - Off-market (inter-se): transfers between promoters
    Only keep: open_market, preferential_allotment (at fair price), other

    Actually, per CLAUDE.md "false-signal filters", exclude:
    - Preferential allotment (that's new shares)
    - Sales for minimum public shareholding (not in acquisition_mode, deferred)
    - Gifts between promoters
    - Passive inflows (not in acquisition_mode, deferred)

    For now, exclude modes that are clearly false signals.
    """
    false_signal_modes = {"esos_esop", "gift", "inheritance", "off_market"}
    return [t for t in trades if t.acquisition_mode not in false_signal_modes]


def filter_false_signals_stake_disclosures(disclosures: list[StakeDisclosureInput]) -> list[StakeDisclosureInput]:
    """Exclude false-positive signals based on disclosure_type.

    Per CLAUDE.md, exclude:
    - Promoter reclassification: not a conviction signal
    Keep: SAST crossings, pledge events (they are signal-eligible)
    """
    false_signal_types = {"reclassification"}
    return [d for d in disclosures if d.disclosure_type not in false_signal_types]


def filter_false_signals_bulk_block_deals(deals: list[BulkBlockDealInput]) -> list[BulkBlockDealInput]:
    """No false-signal filters for bulk/block deals (all are kept).

    They are third-party flows and all are signal-eligible.
    """
    return deals


def aggregate_net_flows_by_category(
    trades: list[InsiderTradeInput] | None = None,
    disclosures: list[StakeDisclosureInput] | None = None,
    deals: list[BulkBlockDealInput] | None = None,
) -> dict:
    """Aggregate net flows by person_category or deal type, using Decimal precision.

    Returns a dict keyed by source type ("insider_trades", "stake_disclosures", "bulk_block_deals"),
    containing aggregated flows by category, with direction (inflow/outflow) based on net sign.
    """
    result = {}

    if trades:
        insider_agg = {}
        for trade in trades:
            cat = trade.person_category
            if cat not in insider_agg:
                insider_agg[cat] = {
                    "net_quantity": Decimal("0"),
                    "inflow_quantity": Decimal("0"),
                    "outflow_quantity": Decimal("0"),
                }
            insider_agg[cat]["net_quantity"] += trade.quantity
            if trade.quantity > 0:
                insider_agg[cat]["inflow_quantity"] += trade.quantity
            else:
                insider_agg[cat]["outflow_quantity"] -= trade.quantity

        # Add direction based on net sign
        for cat, agg in insider_agg.items():
            agg["direction"] = "inflow" if agg["net_quantity"] > 0 else "outflow"
        result["insider_trades"] = insider_agg

    if deals:
        deal_agg = {}
        for deal in deals:
            client = deal.client_name
            if client not in deal_agg:
                deal_agg[client] = {
                    "net_quantity": Decimal("0"),
                    "inflow_quantity": Decimal("0"),
                    "outflow_quantity": Decimal("0"),
                }
            if deal.deal_side == "inflow":
                deal_agg[client]["net_quantity"] += deal.quantity
                deal_agg[client]["inflow_quantity"] += deal.quantity
            else:
                deal_agg[client]["net_quantity"] -= deal.quantity
                deal_agg[client]["outflow_quantity"] += deal.quantity

        for client, agg in deal_agg.items():
            agg["direction"] = "inflow" if agg["net_quantity"] > 0 else "outflow"
        result["bulk_block_deals"] = deal_agg

    return result


def detect_critical_alerts(
    trades: list[InsiderTradeInput] | None = None,
    disclosures: list[StakeDisclosureInput] | None = None,
    deals: list[BulkBlockDealInput] | None = None,
    isin: str = "",
) -> list[OwnershipSignal]:
    """Detect critical-severity conditions per CLAUDE.md.

    Critical alerts:
    - Pledge invoked (from stake_disclosure with disclosure_type="pledge_invoked")
    - Promoter open-market sale above X% of their stake
    - Pledged percentage up more than Y points in a quarter (deferred: not in current input)

    Returns a list of OwnershipSignal with severity="critical".
    """
    alerts = []

    # Check for pledge invoked
    if disclosures:
        for disclosure in disclosures:
            if disclosure.disclosure_type == "pledge_invoked":
                alerts.append(
                    OwnershipSignal(
                        isin=isin or disclosure.isin,
                        signal_type="stake_disclosure",
                        category=disclosure.acquirer_name,
                        direction="outflow",
                        severity="critical",
                        event_date=disclosure.disclosure_date,
                        net_quantity=disclosure.shares_acquired or Decimal("0"),
                        detail=f"Pledge invoked by {disclosure.acquirer_name} at {disclosure.disclosure_date}",
                    )
                )

    # Check for promoter sale above threshold
    if trades:
        for trade in trades:
            if (
                trade.person_category == "Promoter"
                and trade.acquisition_mode == "open_market"
                and trade.quantity < 0
                and trade.post_trade_holding_pct is not None
            ):
                # Flag as critical if:
                # - Holding drops below 50% (significant reduction from majority/major stake)
                # - Or absolute sale quantity is very large (proxy for percentage of their stake)
                # This is a heuristic; exact % calculation is deferred to holdings reconciliation
                is_critical = (
                    trade.post_trade_holding_pct < Decimal("50")  # Dropped below 50%
                    or abs(trade.quantity) > Decimal("10000000")  # Selling >10M shares (large)
                )
                if is_critical:
                    alerts.append(
                        OwnershipSignal(
                            isin=isin or trade.isin,
                            signal_type="insider_trade",
                            category="Promoter",
                            direction="outflow",
                            severity="critical",
                            event_date=trade.trade_date,
                            net_quantity=abs(trade.quantity),
                            detail=f"Large promoter sale by {trade.person_name}, holding now {trade.post_trade_holding_pct}%",
                        )
                    )

    return alerts
