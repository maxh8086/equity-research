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

RULE_VERSION = 'buyback/1'. Look-ahead refusal (R2): inputs with as_of > t
are refused; evaluation at t after record_date is allowed (price risk is
the point). Separate acceptance ratios for general vs reserved categories.
Each category competes only for its own portion using its own ratio.
Past acceptance ratios derived from outcome filings, not made up.
"""

from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import pytest

from core.compute.buyback import (
    BuybackAssumption,
    BuybackOutcomeRecord,
    BuybackType,
    ExpectedReturnResult,
    HolderAllocation,
    HolderCategory,
    OutcomeAssumptionResult,
    compute_expected_return,
    derive_assumption_from_history,
    holder_allocation_report,
    validate_tender_offer,
)


@pytest.fixture
def tender_terms_at_2026_09_15():
    """Tender-offer terms filed on 2026-09-15."""
    return {
        "as_of": datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc),
        "buyback_type": BuybackType.TENDER_OFFER,
        "buyback_price": Decimal("2500.00"),
        "quantity_authorized": 1000000,  # shares
        "record_date": date(2026, 10, 15),
        "tender_window_open": date(2026, 10, 16),
        "tender_window_close": date(2026, 10, 30),
        "reserved_small_shareholder_fraction": Decimal("0.25"),  # 25% reserved
    }


@pytest.fixture
def assumption_with_both_ratios():
    """Assumption with separate acceptance ratios for each category."""
    return BuybackAssumption(
        acceptance_ratio_general=Decimal("0.75"),
        acceptance_ratio_reserved=Decimal("0.80"),
        assumed_exit_price=Decimal("2400.00"),
    )


class TestValidateTenderOffer:
    """Validate tender-offer inputs with explicit evaluation time."""

    def test_tender_filed_before_evaluation_time_accepted(self, tender_terms_at_2026_09_15):
        """Tender filed before evaluation time t is accepted."""
        t = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        result = validate_tender_offer(t=t, **tender_terms_at_2026_09_15)
        assert result is True

    def test_tender_filed_after_evaluation_time_refused(self, tender_terms_at_2026_09_15):
        """Tender filed after evaluation time t is refused (R2 look-ahead)."""
        tender_terms_at_2026_09_15["as_of"] = datetime(
            2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc
        )
        t = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="as_of.*after.*evaluation time"):
            validate_tender_offer(t=t, **tender_terms_at_2026_09_15)

    def test_evaluation_after_record_date_allowed(self, tender_terms_at_2026_09_15):
        """Evaluation at t after record_date IS allowed (price risk is the point)."""
        t = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)  # after record_date
        result = validate_tender_offer(t=t, **tender_terms_at_2026_09_15)
        assert result is True

    def test_record_date_before_tender_window_required(self, tender_terms_at_2026_09_15):
        """Record date must be before or on tender window open."""
        tender_terms_at_2026_09_15["record_date"] = date(2026, 10, 20)
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="record_date.*tender_window"):
            validate_tender_offer(t=t, **tender_terms_at_2026_09_15)

    def test_tender_window_consistency_required(self, tender_terms_at_2026_09_15):
        """Tender window close must be after open."""
        tender_terms_at_2026_09_15["tender_window_close"] = date(2026, 10, 15)
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="tender_window_close.*after.*tender_window_open"):
            validate_tender_offer(t=t, **tender_terms_at_2026_09_15)

    def test_open_market_buyback_rejected(self, tender_terms_at_2026_09_15):
        """Open-market buybacks are tracked but not scored."""
        tender_terms_at_2026_09_15["buyback_type"] = BuybackType.OPEN_MARKET
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="not scored"):
            validate_tender_offer(t=t, **tender_terms_at_2026_09_15)


class TestSeparateAcceptanceRatios:
    """Two independent acceptance ratios: one for general, one for reserved."""

    def test_assumption_has_both_ratios(self, assumption_with_both_ratios):
        """Assumption holds both general and reserved ratios."""
        assert assumption_with_both_ratios.acceptance_ratio_general == Decimal("0.75")
        assert assumption_with_both_ratios.acceptance_ratio_reserved == Decimal("0.80")

    def test_assumption_hash_includes_both_ratios(self, assumption_with_both_ratios):
        """Hash changes if either ratio changes."""
        h1 = assumption_with_both_ratios.assumption_set_hash()

        a2 = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.70"),  # changed
            acceptance_ratio_reserved=Decimal("0.80"),
            assumed_exit_price=Decimal("2400.00"),
        )
        h2 = a2.assumption_set_hash()
        assert h1 != h2

        a3 = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.75"),
            acceptance_ratio_reserved=Decimal("0.85"),  # changed
            assumed_exit_price=Decimal("2400.00"),
        )
        h3 = a3.assumption_set_hash()
        assert h1 != h3

    def test_assumption_hash_includes_rule_version(self, assumption_with_both_ratios):
        """Hash reflects RULE_VERSION in the hash."""
        h1 = assumption_with_both_ratios.assumption_set_hash()
        assert len(h1) == 64  # SHA-256 hex

    def test_general_holder_allocation_only_from_general_portion(self, tender_terms_at_2026_09_15, assumption_with_both_ratios):
        """General category holder competes only for general portion with general ratio."""
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        allocation = holder_allocation_report(
            holder_shares=100,
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            reserved_small_shareholder_fraction=Decimal("0.25"),
            holder_category=HolderCategory.GENERAL,
            acceptance_ratio_general=assumption_with_both_ratios.acceptance_ratio_general,
            acceptance_ratio_reserved=assumption_with_both_ratios.acceptance_ratio_reserved,
        )

        # General holder should only get from general pool (75% of authorized)
        # Using general ratio (0.75)
        assert allocation.general_portion > Decimal("0")
        assert allocation.reserved_portion == Decimal("0")  # zero for general holders

    def test_reserved_holder_allocation_only_from_reserved_portion(self, tender_terms_at_2026_09_15, assumption_with_both_ratios):
        """Reserved category holder competes only for reserved portion with reserved ratio."""
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        allocation = holder_allocation_report(
            holder_shares=100,
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            reserved_small_shareholder_fraction=Decimal("0.25"),
            holder_category=HolderCategory.RESERVED,
            acceptance_ratio_general=assumption_with_both_ratios.acceptance_ratio_general,
            acceptance_ratio_reserved=assumption_with_both_ratios.acceptance_ratio_reserved,
        )

        # Reserved holder: 100 shares × 0.80 (reserved ratio) = 80 shares accepted
        assert allocation.reserved_portion == Decimal("80")
        assert allocation.general_portion == Decimal("0")

    def test_general_holder_allocation_exact(self, tender_terms_at_2026_09_15):
        """General holder allocation is holder_shares × acceptance_ratio_general."""
        allocation = holder_allocation_report(
            holder_shares=100,
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            reserved_small_shareholder_fraction=Decimal("0.25"),
            holder_category=HolderCategory.GENERAL,
            acceptance_ratio_general=Decimal("0.70"),
            acceptance_ratio_reserved=Decimal("0.80"),
        )

        # General holder: 100 shares × 0.70 = 70 shares accepted
        assert allocation.general_portion == Decimal("70")
        assert allocation.reserved_portion == Decimal("0")


class TestExpectedReturnComputation:
    """Expected return accounting for record-date price risk."""

    def test_general_category_uses_general_ratio(self, tender_terms_at_2026_09_15):
        """General category holder uses acceptance_ratio_general."""
        assumption = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.75"),
            acceptance_ratio_reserved=Decimal("0.50"),  # different from general
            assumed_exit_price=Decimal("2400.00"),
        )
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=tender_terms_at_2026_09_15["quantity_authorized"],
            holder_category=HolderCategory.GENERAL,
            assumption=assumption,
            cost_per_share=Decimal("2000.00"),
        )

        # 0.75 × (2500 - 2000) + 0.25 × (2400 - 2000)
        # = 375 + 100 = 475 per share (uses 0.75, not 0.50)
        assert result.expected_return_per_share == Decimal("475.00")

    def test_reserved_category_uses_reserved_ratio(self, tender_terms_at_2026_09_15):
        """Reserved category holder uses acceptance_ratio_reserved."""
        assumption = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.75"),
            acceptance_ratio_reserved=Decimal("0.50"),  # different from general
            assumed_exit_price=Decimal("2400.00"),
        )
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=int(tender_terms_at_2026_09_15["quantity_authorized"]) * 25 // 100,
            holder_category=HolderCategory.RESERVED,
            assumption=assumption,
            cost_per_share=Decimal("2000.00"),
        )

        # 0.50 × (2500 - 2000) + 0.50 × (2400 - 2000)
        # = 250 + 200 = 450 per share (uses 0.50, not 0.75)
        assert result.expected_return_per_share == Decimal("450.00")

    def test_full_acceptance_general_category(self, tender_terms_at_2026_09_15, assumption_with_both_ratios):
        """100% acceptance for general category."""
        assume_full = BuybackAssumption(
            acceptance_ratio_general=Decimal("1.0"),
            acceptance_ratio_reserved=Decimal("1.0"),
            assumed_exit_price=Decimal("2400.00"),
        )
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=Decimal(int(tender_terms_at_2026_09_15["quantity_authorized"]) * 75 // 100),
            holder_category=HolderCategory.GENERAL,
            assumption=assume_full,
            cost_per_share=Decimal("2000.00"),
        )

        # (2500 - 2000) × 1.0 = 500 per share
        assert result.expected_return_per_share == Decimal("500.00")

    def test_zero_acceptance_prices_at_exit(self, tender_terms_at_2026_09_15):
        """0% acceptance: unaccepted shares held at exit price."""
        assume_zero = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.0"),
            acceptance_ratio_reserved=Decimal("0.0"),
            assumed_exit_price=Decimal("2300.00"),
        )
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=tender_terms_at_2026_09_15["quantity_authorized"],
            holder_category=HolderCategory.GENERAL,
            assumption=assume_zero,
            cost_per_share=Decimal("2000.00"),
        )

        # (2300 - 2000) × 1.0 = 300 per share (unaccepted only)
        assert result.expected_return_per_share == Decimal("300.00")

    def test_partial_acceptance_mixed_pricing(self, tender_terms_at_2026_09_15, assumption_with_both_ratios):
        """Partial acceptance: portion at buyback price, remainder at exit price."""
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=tender_terms_at_2026_09_15["quantity_authorized"],
            holder_category=HolderCategory.GENERAL,
            assumption=assumption_with_both_ratios,
            cost_per_share=Decimal("2000.00"),
        )

        # 0.75 × (2500 - 2000) + 0.25 × (2400 - 2000)
        # = 375 + 100 = 475 per share
        assert result.expected_return_per_share == Decimal("475.00")

    def test_price_risk_below_entry_cost(self, tender_terms_at_2026_09_15):
        """Exit price below entry cost reflects downside risk."""
        assume_downside = BuybackAssumption(
            acceptance_ratio_general=Decimal("0.5"),
            acceptance_ratio_reserved=Decimal("0.5"),
            assumed_exit_price=Decimal("1800.00"),  # below entry cost
        )
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=tender_terms_at_2026_09_15["quantity_authorized"],
            holder_category=HolderCategory.GENERAL,
            assumption=assume_downside,
            cost_per_share=Decimal("2000.00"),
        )

        # 0.5 × (2500 - 2000) + 0.5 × (1800 - 2000)
        # = 250 - 100 = 150 per share
        assert result.expected_return_per_share == Decimal("150.00")


class TestOutcomeHistoryToAssumption:
    """Derive assumption from post-buyback outcome filings."""

    def test_no_history_returns_explicit_no_history(self):
        """No past outcomes returns explicit 'no history' result."""
        result = derive_assumption_from_history(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            outcome_records=[],
        )

        assert result.has_history is False
        assert result.assumption is None

    def test_single_outcome_used_as_assumption(self):
        """Single past outcome becomes the assumption."""
        outcome = BuybackOutcomeRecord(
            as_of=datetime(2020, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
            acceptance_ratio_general=Decimal("0.82"),
            acceptance_ratio_reserved=Decimal("0.85"),
        )

        result = derive_assumption_from_history(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            outcome_records=[outcome],
        )

        assert result.has_history is True
        assert result.assumption.acceptance_ratio_general == Decimal("0.82")
        assert result.assumption.acceptance_ratio_reserved == Decimal("0.85")

    def test_multiple_outcomes_mean_used(self):
        """Multiple outcomes: mean ratio is used."""
        outcomes = [
            BuybackOutcomeRecord(
                as_of=datetime(2020, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
                acceptance_ratio_general=Decimal("0.80"),
                acceptance_ratio_reserved=Decimal("0.85"),
            ),
            BuybackOutcomeRecord(
                as_of=datetime(2022, 5, 20, 12, 0, 0, tzinfo=timezone.utc),
                acceptance_ratio_general=Decimal("0.90"),
                acceptance_ratio_reserved=Decimal("0.95"),
            ),
        ]

        result = derive_assumption_from_history(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            outcome_records=outcomes,
        )

        assert result.has_history is True
        # Mean: (0.80 + 0.90) / 2 = 0.85
        assert result.assumption.acceptance_ratio_general == Decimal("0.85")
        # Mean: (0.85 + 0.95) / 2 = 0.90
        assert result.assumption.acceptance_ratio_reserved == Decimal("0.90")

    def test_future_outcomes_excluded(self):
        """Outcomes filed after t are excluded (R2 look-ahead)."""
        outcomes = [
            BuybackOutcomeRecord(
                as_of=datetime(2020, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
                acceptance_ratio_general=Decimal("0.80"),
                acceptance_ratio_reserved=Decimal("0.85"),
            ),
            BuybackOutcomeRecord(
                as_of=datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc),  # after t
                acceptance_ratio_general=Decimal("0.90"),
                acceptance_ratio_reserved=Decimal("0.95"),
            ),
        ]

        t = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        result = derive_assumption_from_history(
            t=t,
            outcome_records=outcomes,
        )

        # Only the first outcome should be used
        assert result.assumption.acceptance_ratio_general == Decimal("0.80")
        assert result.assumption.acceptance_ratio_reserved == Decimal("0.85")


class TestRuleVersion:
    """RULE_VERSION = 'buyback/1' in hash and output."""

    def test_rule_version_constant(self):
        """Module declares RULE_VERSION = 'buyback/1'."""
        from core.compute import buyback

        assert hasattr(buyback, "RULE_VERSION")
        assert buyback.RULE_VERSION == "buyback/1"

    def test_assumption_hash_includes_rule_version(self, assumption_with_both_ratios):
        """Assumption hash is deterministic and includes RULE_VERSION."""
        h1 = assumption_with_both_ratios.assumption_set_hash()
        h2 = assumption_with_both_ratios.assumption_set_hash()
        assert h1 == h2  # Deterministic
        assert len(h1) == 64  # SHA-256 hex

    def test_result_includes_rule_version(self, tender_terms_at_2026_09_15, assumption_with_both_ratios):
        """Expected return result includes rule_version."""
        validate_tender_offer(
            t=datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc),
            **tender_terms_at_2026_09_15
        )

        result = compute_expected_return(
            buyback_price=tender_terms_at_2026_09_15["buyback_price"],
            quantity_authorized=tender_terms_at_2026_09_15["quantity_authorized"],
            quantity_available_to_category=tender_terms_at_2026_09_15["quantity_authorized"],
            holder_category=HolderCategory.GENERAL,
            assumption=assumption_with_both_ratios,
            cost_per_share=Decimal("2000.00"),
        )

        assert result.rule_version == "buyback/1"
