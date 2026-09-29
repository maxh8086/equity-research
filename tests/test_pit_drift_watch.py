"""Tests for drift_watch_as_of: point-in-time reader for post-earnings drift.

DB-backed tests with session rollback. All metric inputs are computed from
stored facts and guidance resolutions at the observed time `as_of`.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from core.compute.drift import DriftWatch, MissingInputReason
from core.db.pit import drift_watch_as_of, drift_watchlist_as_of, DriftWatchlistEntry
from core.db.models import Consolidation
from core.timezones import IST
from tests.factories import RELIANCE, make_fact

pytestmark = pytest.mark.db


class TestDriftWatchBasic:
    """Test basic drift_watch_as_of functionality."""

    def test_missing_all_inputs_returns_watch_with_missing_reasons(self, session):
        """Without facts or guidance, returns a DriftWatch with missing_reasons."""
        t = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=date(2024, 12, 31),
            as_of=t,
            evidence_url="https://example.com/results",
        )

        assert isinstance(watch, DriftWatch)
        assert watch.isin == RELIANCE
        assert watch.period_end == date(2024, 12, 31)
        assert watch.observed_on == t.date()
        assert watch.yoy_sales_growth_pct is None
        assert watch.yoy_pat_growth_pct is None
        # Should have missing reasons for required metrics
        assert MissingInputReason.MISSING_SALES_DATA in watch.missing_reasons
        assert MissingInputReason.MISSING_PROFIT_DATA in watch.missing_reasons

    def test_returns_watch_with_computed_yoy_metrics(self, session):
        """Computes and returns YoY growth from consecutive periods."""
        prior_year = date(2023, 12, 31)
        current_year = date(2024, 12, 31)
        as_of_time = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

        # Prior year facts (calendar year 2023)
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2023, 1, 1),
                period_end=prior_year,
                line_item="total_revenue",
                value=Decimal("100000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2023, 1, 1),
                period_end=prior_year,
                line_item="profit_after_tax",
                value=Decimal("10000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )

        # Current year facts (calendar year 2024) - 20% growth
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2024, 1, 1),
                period_end=current_year,
                line_item="total_revenue",
                value=Decimal("120000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2024, 1, 1),
                period_end=current_year,
                line_item="profit_after_tax",
                value=Decimal("12000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )

        session.flush()

        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=current_year,
            as_of=as_of_time,
            evidence_url="https://example.com/results",
        )

        # Check YoY growth is computed (20% for both)
        assert watch.yoy_sales_growth_pct == Decimal("20.00")
        assert watch.yoy_pat_growth_pct == Decimal("20.00")
        # Score should be non-zero
        assert watch.score > 0

    def test_respects_as_of_boundary(self, session):
        """Only reads data with as_of <= the observation time."""
        period_end = date(2024, 12, 31)
        # Add a fact with as_of after period_end (as required by constraint)
        fact_as_of = datetime(2025, 1, 10, 12, 0, tzinfo=IST)
        session.add(
            make_fact(
                isin=RELIANCE,
                period_end=period_end,
                line_item="total_revenue",
                value=Decimal("100000"),
                as_of=fact_as_of,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.flush()

        # Read before that fact's as_of date
        t_before = datetime(2025, 1, 9, 23, 59, tzinfo=IST)
        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=period_end,
            as_of=t_before,
            evidence_url="https://example.com/results",
        )

        # Should not see the fact yet (as_of of fact is after observation time)
        assert MissingInputReason.MISSING_SALES_DATA in watch.missing_reasons


class TestDriftWatchEdgeCases:
    """Test edge cases and error conditions."""

    def test_negative_yoy_growth_triggers_loss_to_profit(self, session):
        """Negative YoY PAT signals loss-to-profit scenario."""
        prior_year = date(2023, 12, 31)
        current_year = date(2024, 12, 31)
        as_of_time = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

        # Prior year: loss-making (negative profit)
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2023, 1, 1),
                period_end=prior_year,
                line_item="profit_after_tax",
                value=Decimal("-10000"),  # Loss
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )

        # Current year: some profit (growth not computable from negative base)
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2024, 1, 1),
                period_end=current_year,
                line_item="profit_after_tax",
                value=Decimal("5000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )

        session.flush()

        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=current_year,
            as_of=as_of_time,
            evidence_url="https://example.com/results",
        )

        # With negative prior year PAT, growth is not computed
        assert watch.yoy_pat_growth_pct is None
        assert MissingInputReason.LOSS_TO_PROFIT_PROFIT in watch.missing_reasons

    def test_other_isin_not_leaked(self, session):
        """Data for other ISINs is not included."""
        as_of_time = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
        period_end = date(2024, 12, 31)

        # Add facts for a different ISIN
        other_isin = "INE467B01029"  # TCS
        session.add(
            make_fact(
                isin=other_isin,
                period_end=period_end,
                line_item="total_revenue",
                value=Decimal("100000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.flush()

        # Read for RELIANCE should not see TCS data
        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=period_end,
            as_of=as_of_time,
            evidence_url="https://example.com/results",
        )

        assert MissingInputReason.MISSING_SALES_DATA in watch.missing_reasons

    def test_naive_as_of_raises(self, session):
        """Naive (non-aware) datetime as_of raises ValueError."""
        with pytest.raises(ValueError, match="timezone-aware"):
            drift_watch_as_of(
                session,
                isin=RELIANCE,
                period_end=date(2024, 12, 31),
                as_of=datetime(2025, 1, 15),  # Naive datetime
                evidence_url="https://example.com/results",
            )

    def test_missing_trailing_trend_and_guidance_explicitly_marked(self, session):
        """Unimplemented metrics explicitly yield missing reasons, not TODOs."""
        t = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

        watch = drift_watch_as_of(
            session,
            isin=RELIANCE,
            period_end=date(2024, 12, 31),
            as_of=t,
            evidence_url="https://example.com/results",
        )

        # Guidance surprise should be marked as missing (not silent TODO)
        assert MissingInputReason.NO_GUIDANCE_CLAIMS in watch.missing_reasons
        # Trailing trend is optional, so not in missing_reasons if not computed
        # Forward PE missing reasons handled via UNKNOWN_SHARE_COUNT when data gaps exist


class TestDriftWatchlistWiring:
    """Test watchlist wiring: drift as a read-only source."""

    def test_drift_watchlist_returns_empty_without_filings(self, session):
        """Without financial filings, drift_watchlist returns empty list."""
        t = datetime(2025, 1, 15, 12, 0, tzinfo=IST)

        entries = drift_watchlist_as_of(session, isin=RELIANCE, as_of=t)

        assert entries == []

    def test_drift_watchlist_entry_wraps_drift_watch(self, session):
        """drift_watchlist_as_of returns DriftWatchlistEntry(drift=DriftWatch)."""
        as_of_time = datetime(2025, 1, 15, 12, 0, tzinfo=IST)
        period_end = date(2024, 12, 31)

        # Add facts and a filing to enable drift computation
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2024, 1, 1),
                period_end=period_end,
                line_item="total_revenue",
                value=Decimal("120000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.add(
            make_fact(
                isin=RELIANCE,
                period_start=date(2024, 1, 1),
                period_end=period_end,
                line_item="profit_after_tax",
                value=Decimal("12000"),
                as_of=as_of_time,
                consolidation=Consolidation.CONSOLIDATED,
            )
        )
        session.flush()

        entries = drift_watchlist_as_of(session, isin=RELIANCE, as_of=as_of_time)

        # Should return a list with one DriftWatchlistEntry
        assert len(entries) == 1
        assert isinstance(entries[0], DriftWatchlistEntry)
        assert isinstance(entries[0].drift, DriftWatch)
        assert entries[0].source == "drift/1"
