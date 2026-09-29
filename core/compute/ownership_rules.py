"""Ownership rules: false-signal filters, net aggregation, threshold detection, critical alerts.

Pure, fully tested. Input is frozen dataclasses mirroring the DB models.
Output: inflow/outflow signals only (no action verdicts).
Thresholds (X: promoter sale %, Y: pledge % increase) are REQUIRED parameters, never invented.
Unmapped modes are tagged 'unclassified' for manual review, never excluded by guess.
Not-evaluable results returned when data is insufficient, never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

RULE_VERSION = "ownership_rules/1"

# ========================================================================
# EXPLICIT CLASSIFICATION MAPPINGS: hardcoded per RULE_VERSION
# Per CLAUDE.md § Ownership, false-signal filters:
# ========================================================================

ACQUISITION_MODE_FALSE_SIGNALS = {
    "esos_esop": "new shares diluting everyone",
    "preferential_allotment": "new shares diluting everyone",
    "gift": "transfer or gift between people",
    "inheritance": "transfer or gift between people",
    # off_market: CANNOT assume inter-promoter without counterparty data → unclassified
    # pledge_invocation: unknown → unclassified
    # other: unknown → unclassified
}

DISCLOSURE_TYPE_FALSE_SIGNALS = {
    "reclassification": "promoter reclassification (not conviction signal)",
    # SAST crossings, pledge_created, pledge_released, pledge_invoked: all signal-eligible
}


@dataclass(frozen=True)
class InsiderTradeInput:
    """Promoter / insider trade disclosure, mirroring InsiderTrade model fields."""

    isin: str
    person_name: str
    person_category: str
    acquisition_mode: str
    trade_date: date
    quantity: Decimal  # Positive for buy, negative for sell
    price_per_share: Decimal
    post_trade_holding_pct: Decimal | None


@dataclass(frozen=True)
class StakeDisclosureInput:
    """Large-stake crossing or pledge event disclosure."""

    isin: str
    acquirer_name: str
    disclosure_type: str
    disclosure_date: date
    shares_acquired: Decimal | None
    post_acquisition_pct: Decimal | None


@dataclass(frozen=True)
class BulkBlockDealInput:
    """Bulk or block deal record."""

    isin: str
    deal_type: str
    deal_date: date
    client_name: str
    quantity: Decimal
    price_per_share: Decimal
    deal_side: str


@dataclass(frozen=True)
class ClassifiedTrade:
    """A trade with its classification status."""

    trade: InsiderTradeInput | StakeDisclosureInput
    classification: Literal["signal_eligible", "false_signal", "unclassified"]
    reason: str | None = None


@dataclass(frozen=True)
class NotEvaluableResult:
    """Result when evaluation cannot be completed due to insufficient data."""

    isin: str
    event_type: str
    reason: str
    event_date: date


@dataclass(frozen=True)
class OwnershipSignal:
    """A single ownership signal: inflow/outflow direction only."""

    isin: str
    signal_type: str
    category: str
    direction: str  # "inflow" or "outflow"
    severity: str  # "critical", "high", "medium", "low"
    event_date: date
    net_quantity: Decimal
    detail: str


def filter_false_signals_insider_trades(
    trades: list[InsiderTradeInput],
) -> list[ClassifiedTrade]:
    """Classify trades as signal-eligible, false-signal, or unclassified.

    Per RULE_VERSION mapping, exclude modes in ACQUISITION_MODE_FALSE_SIGNALS.
    Unmapped modes (off_market, pledge_invocation, other) are tagged 'unclassified'.
    Never exclude by guess; always provide explicit classification.
    """
    result = []
    for trade in trades:
        if trade.acquisition_mode in ACQUISITION_MODE_FALSE_SIGNALS:
            result.append(
                ClassifiedTrade(
                    trade=trade,
                    classification="false_signal",
                    reason=ACQUISITION_MODE_FALSE_SIGNALS[trade.acquisition_mode],
                )
            )
        elif trade.acquisition_mode == "open_market":
            result.append(
                ClassifiedTrade(
                    trade=trade,
                    classification="signal_eligible",
                    reason="open market trade",
                )
            )
        else:
            # Unknown mode: classify as unclassified, don't exclude
            result.append(
                ClassifiedTrade(
                    trade=trade,
                    classification="unclassified",
                    reason=f"acquisition_mode '{trade.acquisition_mode}' not mapped; requires manual review",
                )
            )
    return result


def filter_false_signals_stake_disclosures(
    disclosures: list[StakeDisclosureInput],
) -> list[ClassifiedTrade]:
    """Classify stake disclosures as signal-eligible, false-signal, or unclassified."""
    result = []
    for disclosure in disclosures:
        if disclosure.disclosure_type in DISCLOSURE_TYPE_FALSE_SIGNALS:
            result.append(
                ClassifiedTrade(
                    trade=disclosure,
                    classification="false_signal",
                    reason=DISCLOSURE_TYPE_FALSE_SIGNALS[disclosure.disclosure_type],
                )
            )
        else:
            # SAST, pledge_created, pledge_released, pledge_invoked: all signal-eligible
            result.append(
                ClassifiedTrade(
                    trade=disclosure,
                    classification="signal_eligible",
                    reason=f"disclosure_type '{disclosure.disclosure_type}' is signal-eligible",
                )
            )
    return result


def aggregate_net_flows_by_category(
    classified_trades: list[ClassifiedTrade] | None = None,
    classified_disclosures: list[ClassifiedTrade] | None = None,
    deals: list[BulkBlockDealInput] | None = None,
) -> dict:
    """Aggregate net flows by category, including only signal-eligible items.

    Unclassified and false-signal items are excluded from aggregation.
    Returns dict with aggregated flows keyed by source type.
    """
    result = {}

    if classified_trades:
        insider_agg = {}
        for ct in classified_trades:
            # Only aggregate signal-eligible trades
            if ct.classification != "signal_eligible":
                continue

            trade = ct.trade
            if not isinstance(trade, InsiderTradeInput):
                continue

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

        for cat, agg in insider_agg.items():
            agg["direction"] = "inflow" if agg["net_quantity"] > 0 else "outflow"

        result["insider_trades"] = insider_agg

    if classified_disclosures:
        disclosure_agg = {}
        for cd in classified_disclosures:
            # Only aggregate signal-eligible disclosures
            if cd.classification != "signal_eligible":
                continue

            disclosure = cd.trade
            if not isinstance(disclosure, StakeDisclosureInput):
                continue

            acquirer = disclosure.acquirer_name
            if acquirer not in disclosure_agg:
                disclosure_agg[acquirer] = {
                    "net_quantity": Decimal("0"),
                    "acquisition_pct": disclosure.post_acquisition_pct,
                }
            # For disclosures, track shares if available
            if disclosure.shares_acquired:
                disclosure_agg[acquirer]["net_quantity"] += disclosure.shares_acquired

        result["stake_disclosures"] = disclosure_agg

    return result


def detect_critical_alerts(
    trades: list[InsiderTradeInput] | None = None,
    classified_trades: list[ClassifiedTrade] | None = None,
    disclosures: list[StakeDisclosureInput] | None = None,
    classified_disclosures: list[ClassifiedTrade] | None = None,
    isin: str = "",
    promoter_sale_pct_threshold: Decimal | None = None,
) -> list[OwnershipSignal | NotEvaluableResult]:
    """Detect critical-severity conditions per CLAUDE.md.

    Critical alerts:
    - Pledge invoked: explicit critical per CLAUDE.md
    - Promoter sale above X% of stake: X is REQUIRED; returns not-evaluable if data insufficient

    Returns list of OwnershipSignal (critical alerts) or NotEvaluableResult (insufficient data).
    Never guesses; always explicit about what cannot be evaluated.
    """
    results: list[OwnershipSignal | NotEvaluableResult] = []

    # Check for pledge invoked (explicit critical per CLAUDE.md)
    if disclosures:
        for disclosure in disclosures:
            if disclosure.disclosure_type == "pledge_invoked":
                results.append(
                    OwnershipSignal(
                        isin=isin or disclosure.isin,
                        signal_type="stake_disclosure",
                        category=disclosure.acquirer_name,
                        direction="outflow",
                        severity="critical",
                        event_date=disclosure.disclosure_date,
                        net_quantity=disclosure.shares_acquired or Decimal("0"),
                        detail=f"Pledge invoked by {disclosure.acquirer_name}",
                    )
                )

    if classified_disclosures:
        for cd in classified_disclosures:
            disclosure = cd.trade
            if not isinstance(disclosure, StakeDisclosureInput):
                continue
            if disclosure.disclosure_type == "pledge_invoked":
                results.append(
                    OwnershipSignal(
                        isin=isin or disclosure.isin,
                        signal_type="stake_disclosure",
                        category=disclosure.acquirer_name,
                        direction="outflow",
                        severity="critical",
                        event_date=disclosure.disclosure_date,
                        net_quantity=disclosure.shares_acquired or Decimal("0"),
                        detail=f"Pledge invoked by {disclosure.acquirer_name}",
                    )
                )

    # Check for large promoter sales
    # X (promoter_sale_pct_threshold) is REQUIRED; without it, cannot evaluate
    if trades or classified_trades:
        trades_to_check = []
        if classified_trades:
            for ct in classified_trades:
                if isinstance(ct.trade, InsiderTradeInput):
                    trades_to_check.append(ct.trade)
        else:
            trades_to_check = trades or []

        for trade in trades_to_check:
            if (
                trade.person_category == "Promoter"
                and trade.acquisition_mode == "open_market"
                and trade.quantity < 0
            ):
                # To compute sale as % of their stake, we need pre-trade holding
                # We only have post_trade_holding_pct, not pre_trade_holding_pct
                # Without total shares outstanding, cannot compute this
                if not promoter_sale_pct_threshold:
                    results.append(
                        NotEvaluableResult(
                            isin=isin or trade.isin,
                            event_type="promoter_sale_pct_threshold",
                            reason="promoter_sale_pct_threshold parameter required but not provided",
                            event_date=trade.trade_date,
                        )
                    )
                    continue

                # Even with threshold, we lack pre-trade holding to compute actual %
                results.append(
                    NotEvaluableResult(
                        isin=isin or trade.isin,
                        event_type="promoter_sale_pct_of_stake",
                        reason="Cannot compute sale as % of stake: pre-trade holding not in input data",
                        event_date=trade.trade_date,
                    )
                )

    return results
