"""Buyback tender-offer expected-return computation.

Tender-offer filing: buyback price, size, record date, tender window and
reserved small-shareholder portion.

Expected return is computed by code as
`accepted × (buyback_price − cost) + unaccepted × (assumed_exit − cost)`.
The acceptance ratio is not knowable at entry, so it is a stored assumption
with its assumption-set hash. Past ratios come from post-buyback outcome
filings.

Unaccepted shares carry price risk after the record date and the calculation
says so. Open-market buybacks are tracked but never scored this way.
Per-holder allocation is a report; a human tenders in the broker's app.

Look-ahead refusal (R2): every INPUT carries its own as_of and is refused if
dated after `t` (evaluation time). Evaluation at `t` after record_date IS
allowed — that's where price risk appears.

Separate acceptance ratios for general vs reserved holder categories.
Each category competes only for its own portion using its own ratio.
Holder category is an input flag; code does not guess.

Past acceptance ratios derived from outcome filings (mean of ratios known at t),
not made up. Returns explicit 'no history' result if no past outcomes exist.

Pure functions over frozen dataclasses (no DB/IO/LLM; Decimal, no float;
inputs carry as_of, refuse inputs dated after t).

RULE_VERSION = 'buyback/1'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from core.compute.hashing import content_hash


RULE_VERSION = "buyback/1"


class BuybackType(StrEnum):
    """Buyback classification."""

    TENDER_OFFER = "tender_offer"
    OPEN_MARKET = "open_market"


class HolderCategory(StrEnum):
    """Holder category for allocation purpose."""

    GENERAL = "general"
    RESERVED = "reserved"  # small shareholder


@dataclass(frozen=True)
class BuybackAssumption:
    """Assumptions for buyback return calculation.

    Separate acceptance ratios for general and reserved categories.
    Each category competes only for its own portion using its own ratio.
    """

    acceptance_ratio_general: Decimal
    """Fraction of general-category tendered shares that are accepted (0.0 to 1.0)."""

    acceptance_ratio_reserved: Decimal
    """Fraction of reserved-category (small shareholder) tendered shares accepted (0.0 to 1.0)."""

    assumed_exit_price: Decimal
    """Expected price for unaccepted shares after record date (in rupees)."""

    def assumption_set_hash(self) -> str:
        """SHA-256 hash of the assumption set for replay validation.

        Includes every assumption field plus RULE_VERSION.
        Changes when any assumption changes, allowing the system to detect
        when an assumption set has been revised and re-run earlier analysis.
        """
        # Canonical string representation: sorted keys, deterministic format
        data_str = (
            f"acceptance_ratio_general={self.acceptance_ratio_general!s}\n"
            f"acceptance_ratio_reserved={self.acceptance_ratio_reserved!s}\n"
            f"assumed_exit_price={self.assumed_exit_price!s}\n"
            f"rule_version={RULE_VERSION}"
        )
        return content_hash(data_str.encode("utf-8"))


@dataclass(frozen=True)
class BuybackOutcomeRecord:
    """Post-buyback outcome filing: actual acceptance ratios achieved."""

    as_of: datetime
    """Filing date of the outcome (as_of in temporal model)."""

    acceptance_ratio_general: Decimal
    """Actual fraction of general-category shares accepted."""

    acceptance_ratio_reserved: Decimal
    """Actual fraction of reserved-category shares accepted."""


@dataclass(frozen=True)
class OutcomeAssumptionResult:
    """Result of deriving assumption from outcome history."""

    has_history: bool
    """Whether any past outcomes exist (known at t)."""

    assumption: BuybackAssumption | None
    """Derived assumption, or None if no history."""


@dataclass(frozen=True)
class ExpectedReturnResult:
    """Expected return from a buyback tender offer.

    Computed as: accepted × (buyback_price − cost) + unaccepted × (assumed_exit − cost)
    """

    expected_return_inr: Decimal
    """Total expected return in rupees for the quantity available to this category."""

    expected_return_per_share: Decimal
    """Expected return per share (in rupees)."""

    assumption_set_hash: str
    """Hash of the assumption set for replay validation."""

    rule_version: str = RULE_VERSION
    """Rule version for this calculation."""


@dataclass(frozen=True)
class HolderAllocation:
    """Per-holder share allocation across categories.

    A human tenders in the broker's app; this is a report function.
    Each holder in a category competes only for that category's portion
    using that category's acceptance ratio.
    """

    general_portion: Decimal
    """Shares allocated from the general (non-reserved) portion."""

    reserved_portion: Decimal
    """Shares allocated from the reserved small-shareholder portion."""


def validate_tender_offer(
    t: datetime,
    as_of: datetime,
    buyback_type: BuybackType,
    buyback_price: Decimal,
    quantity_authorized: int,
    record_date: date,
    tender_window_open: date,
    tender_window_close: date,
    reserved_small_shareholder_fraction: Decimal,
) -> bool:
    """Validate tender-offer inputs. Refuse open-market and look-ahead.

    Look-ahead refusal (R2): inputs with as_of > t are refused; evaluation
    at t after record_date IS allowed (price risk is the point).

    Raises ValueError if:
    - buyback_type is OPEN_MARKET (never scored this way, per CLAUDE.md)
    - as_of is after t (look-ahead refusal, R2)
    - record_date is after tender_window_open
    - tender_window_close is before tender_window_open
    - quantity_authorized <= 0
    - reserved_small_shareholder_fraction not in [0, 1]
    - buyback_price < 0
    """
    # Refuse open-market buybacks: tracked but not scored
    if buyback_type == BuybackType.OPEN_MARKET:
        raise ValueError("Open-market buybacks are tracked but not scored this way (CLAUDE.md ⑮)")

    # Look-ahead refusal: as_of must not be after t (R2)
    as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
    t_date = t.date() if isinstance(t, datetime) else t
    if as_of_date > t_date:
        raise ValueError(
            f"as_of date {as_of_date} is after evaluation time {t_date}: "
            "look-ahead is not allowed (R2)"
        )

    # Temporal ordering: record date before tender window open
    if record_date > tender_window_open:
        raise ValueError(
            f"record_date {record_date} must be on or before "
            f"tender_window_open {tender_window_open}"
        )

    # Tender window consistency
    if tender_window_close < tender_window_open:
        raise ValueError(
            f"tender_window_close {tender_window_close} must be after "
            f"tender_window_open {tender_window_open}"
        )

    # Quantity constraints
    if quantity_authorized <= 0:
        raise ValueError(f"quantity_authorized must be positive, got {quantity_authorized}")

    # Fraction constraints
    if not (Decimal("0") <= reserved_small_shareholder_fraction <= Decimal("1")):
        raise ValueError(
            f"reserved_small_shareholder_fraction must be between 0 and 1, "
            f"got {reserved_small_shareholder_fraction}"
        )

    # Price constraints
    if buyback_price < Decimal("0"):
        raise ValueError(f"buyback_price must be non-negative, got {buyback_price}")

    return True


def compute_expected_return(
    buyback_price: Decimal,
    quantity_authorized: int,
    quantity_available_to_category: int,
    holder_category: HolderCategory,
    assumption: BuybackAssumption,
    cost_per_share: Decimal,
) -> ExpectedReturnResult:
    """Compute expected return for a tender-offer buyback category.

    Expected return = accepted × (buyback_price − cost) + unaccepted × (assumed_exit − cost)

    Where:
    - For GENERAL category: acceptance ratio is assumption.acceptance_ratio_general
    - For RESERVED category: acceptance ratio is assumption.acceptance_ratio_reserved
    - accepted = quantity_available_to_category × acceptance_ratio
    - unaccepted = quantity_available_to_category × (1 - acceptance_ratio)

    Args:
        buyback_price: Price the company is offering per share.
        quantity_authorized: Total shares the company authorized for buyback.
        quantity_available_to_category: Shares available to this holder's category.
        holder_category: Whether this is GENERAL or RESERVED category.
        assumption: Assumptions including both acceptance ratios and exit price.
        cost_per_share: Entry cost of the holding.

    Returns:
        ExpectedReturnResult with total and per-share returns.
    """
    # Decimal arithmetic, never float
    # Select the ratio based on holder category
    if holder_category == HolderCategory.GENERAL:
        acceptance_ratio = assumption.acceptance_ratio_general
    else:  # RESERVED
        acceptance_ratio = assumption.acceptance_ratio_reserved

    qty = Decimal(quantity_available_to_category)
    accepted_qty = qty * acceptance_ratio
    unaccepted_qty = qty * (Decimal("1") - acceptance_ratio)

    # Return from accepted shares: at buyback price
    accepted_return_per_share = buyback_price - cost_per_share
    accepted_total = accepted_qty * accepted_return_per_share

    # Return from unaccepted shares: at assumed exit price
    unaccepted_return_per_share = assumption.assumed_exit_price - cost_per_share
    unaccepted_total = unaccepted_qty * unaccepted_return_per_share

    # Total expected return
    total_return = accepted_total + unaccepted_total
    per_share_return = total_return / qty if qty > 0 else Decimal("0")

    return ExpectedReturnResult(
        expected_return_inr=total_return,
        expected_return_per_share=per_share_return,
        assumption_set_hash=assumption.assumption_set_hash(),
        rule_version=RULE_VERSION,
    )


def holder_allocation_report(
    holder_shares: int,
    quantity_authorized: int,
    reserved_small_shareholder_fraction: Decimal,
    holder_category: HolderCategory,
    acceptance_ratio_general: Decimal,
    acceptance_ratio_reserved: Decimal,
) -> HolderAllocation:
    """Per-holder share allocation for each category.

    Each category competes only for its own portion using its own acceptance ratio.
    The broker/registrar defines which category a holder belongs to; code does not guess.

    General holders:
    - compete for (1 - reserved_fraction) of authorized quantity
    - use acceptance_ratio_general

    Reserved holders:
    - compete for reserved_fraction of authorized quantity
    - use acceptance_ratio_reserved

    A holder in a category gets zero allocation from the other category's portion.

    Args:
        holder_shares: Number of shares this holder owns.
        quantity_authorized: Total shares the company authorized for buyback.
        reserved_small_shareholder_fraction: Fraction of authorized reserved for small holders.
        holder_category: Whether this holder is GENERAL or RESERVED.
        acceptance_ratio_general: Assumption for general category acceptance.
        acceptance_ratio_reserved: Assumption for reserved category acceptance.

    Returns:
        HolderAllocation with general and reserved portions.
    """
    holder_decimal = Decimal(holder_shares)

    if holder_category == HolderCategory.GENERAL:
        # General holder: can tender all their shares; acceptance_ratio_general determines allocation
        general_allocation = holder_decimal * acceptance_ratio_general
        reserved_allocation = Decimal("0")
    else:  # RESERVED
        # Reserved holder: can tender all their shares; acceptance_ratio_reserved determines allocation
        general_allocation = Decimal("0")
        reserved_allocation = holder_decimal * acceptance_ratio_reserved

    return HolderAllocation(
        general_portion=general_allocation,
        reserved_portion=reserved_allocation,
    )


def derive_assumption_from_history(
    t: datetime,
    outcome_records: list[BuybackOutcomeRecord],
) -> OutcomeAssumptionResult:
    """Derive assumption from post-buyback outcome filings.

    Past acceptance ratios are known at t; outcomes filed after t are excluded (R2).
    If multiple past outcomes exist, their mean is used.
    If no past outcomes exist (before t), returns explicit 'no history' result.

    Args:
        t: Evaluation time. Outcomes with as_of > t are excluded.
        outcome_records: Past buyback outcome filings with actual acceptance ratios.

    Returns:
        OutcomeAssumptionResult with has_history flag and derived assumption (or None).
    """
    # Filter outcomes known at t (as_of <= t)
    known_outcomes = [r for r in outcome_records if r.as_of <= t]

    if not known_outcomes:
        return OutcomeAssumptionResult(has_history=False, assumption=None)

    # Mean of known outcomes
    general_sum = sum(r.acceptance_ratio_general for r in known_outcomes)
    reserved_sum = sum(r.acceptance_ratio_reserved for r in known_outcomes)
    count = Decimal(len(known_outcomes))

    assumption = BuybackAssumption(
        acceptance_ratio_general=general_sum / count,
        acceptance_ratio_reserved=reserved_sum / count,
        assumed_exit_price=Decimal("0"),  # Not derivable from outcomes; caller provides
    )

    return OutcomeAssumptionResult(has_history=True, assumption=assumption)
