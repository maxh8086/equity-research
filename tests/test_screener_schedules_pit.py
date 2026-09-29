"""Tests for PIT reader for idempotent Screener schedules reruns.

Tests-first for the pit.py addition that checks if a (isin, period_end, line_item)
fact has already been written by this adapter.
"""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from core.db.models import FinancialFact, Consolidation, FactKind
from core.db.pit import screener_schedules_fact_as_of, screener_schedules_series_as_of
from core.timezones import IST


class TestScreenerSchedulesPIT:
    """Test PIT reader for idempotent reruns."""

    def test_fact_not_previously_written(self, session: Session):
        """Return None when fact has not been written before."""
        isin = "INE467B01029"
        period_end = date(2025, 3, 31)
        line_item = "property_plant_and_equipment_gross"
        as_of = datetime(2026, 9, 29, 12, 0, 0, tzinfo=IST)

        result = screener_schedules_fact_as_of(
            session, isin=isin, period_end=period_end, line_item=line_item, as_of=as_of
        )

        assert result is None

    def test_fact_previously_written(self, session: Session):
        """Return fact when it has been written before."""
        isin = "INE467B01029"
        period_end = date(2025, 3, 31)
        line_item = "property_plant_and_equipment_gross"
        value = 3456780000000  # 345,678 crore
        as_of = datetime(2026, 9, 29, 12, 0, 0, tzinfo=IST)

        # Write a fact to the DB
        session.add(
            FinancialFact(
                isin=isin,
                consolidation=Consolidation.CONSOLIDATED,
                fact_kind=FactKind.COMPUTED,
                line_item=line_item,
                xbrl_element=None,
                period_start=None,
                period_end=period_end,
                value=value,
                unit="INR",
                rule_version="screener_schedules/1",
                as_of=as_of,
                content_hash="0" * 64,
                source_url="https://www.screener.in/api/company/3365/schedules/",
                extracted_by="ingest.screener_schedules.adapters.ScreenerSchedules",
                model_version=None,
            )
        )
        session.flush()

        # Query for it
        result = screener_schedules_fact_as_of(
            session, isin=isin, period_end=period_end, line_item=line_item, as_of=as_of
        )

        assert result is not None
        assert result.value == value

    def test_filters_by_isin(self, session: Session):
        """Query filtered by ISIN."""
        as_of = datetime(2026, 9, 29, 12, 0, 0, tzinfo=IST)

        # Write a fact for one ISIN
        session.add(
            FinancialFact(
                isin="INE467B01029",
                consolidation=Consolidation.CONSOLIDATED,
                fact_kind=FactKind.COMPUTED,
                line_item="property_plant_and_equipment_gross",
                xbrl_element=None,
                period_start=None,
                period_end=date(2025, 3, 31),
                value=100,
                unit="INR",
                rule_version="screener_schedules/1",
                as_of=as_of,
                content_hash="0" * 64,
                source_url="test",
                extracted_by="ingest.screener_schedules.adapters.ScreenerSchedules",
                model_version=None,
            )
        )
        session.flush()

        # Query for a different ISIN
        result = screener_schedules_fact_as_of(
            session,
            isin="INE002A01012",  # Different ISIN
            period_end=date(2025, 3, 31),
            line_item="property_plant_and_equipment_gross",
            as_of=as_of,
        )

        assert result is None

    def test_filters_by_period_end(self, session: Session):
        """Query filtered by period_end."""
        isin = "INE467B01029"
        as_of = datetime(2026, 9, 29, 12, 0, 0, tzinfo=IST)

        # Write a fact for one period
        session.add(
            FinancialFact(
                isin=isin,
                consolidation=Consolidation.CONSOLIDATED,
                fact_kind=FactKind.COMPUTED,
                line_item="property_plant_and_equipment_gross",
                xbrl_element=None,
                period_start=None,
                period_end=date(2025, 3, 31),
                value=100,
                unit="INR",
                rule_version="screener_schedules/1",
                as_of=as_of,
                content_hash="0" * 64,
                source_url="test",
                extracted_by="ingest.screener_schedules.adapters.ScreenerSchedules",
                model_version=None,
            )
        )
        session.flush()

        # Query for a different period
        result = screener_schedules_fact_as_of(
            session,
            isin=isin,
            period_end=date(2024, 3, 31),  # Different period
            line_item="property_plant_and_equipment_gross",
            as_of=as_of,
        )

        assert result is None


_LINE = "property_plant_and_equipment_gross"
_EXTRACTOR = "ingest.screener_schedules.adapters.ScreenerSchedules"


def _put(session, *, isin="INE467B01029", period_end, value, as_of, extracted_by=_EXTRACTOR):
    session.add(
        FinancialFact(
            isin=isin,
            consolidation=Consolidation.CONSOLIDATED,
            fact_kind=FactKind.COMPUTED,
            line_item=_LINE,
            xbrl_element=None,
            period_start=None,
            period_end=period_end,
            value=value,
            unit="INR",
            rule_version="screener_schedules/1",
            as_of=as_of,
            content_hash="0" * 64,
            source_url="test",
            extracted_by=extracted_by,
            model_version=None,
        )
    )
    session.flush()


class TestScreenerSchedulesSeries:
    """Series reader: one row per period, latest version known at as_of, oldest first."""

    T1 = datetime(2026, 6, 1, 12, 0, tzinfo=IST)
    T2 = datetime(2026, 9, 1, 12, 0, tzinfo=IST)
    ASOF = datetime(2026, 9, 29, 12, 0, tzinfo=IST)

    def test_returns_every_period_oldest_first(self, session: Session):
        _put(session, period_end=date(2025, 3, 31), value=300, as_of=self.T1)
        _put(session, period_end=date(2023, 3, 31), value=100, as_of=self.T1)
        _put(session, period_end=date(2024, 3, 31), value=200, as_of=self.T1)

        series = screener_schedules_series_as_of(
            session, isin="INE467B01029", line_item=_LINE, as_of=self.ASOF
        )

        assert [(f.period_end, f.value) for f in series] == [
            (date(2023, 3, 31), 100),
            (date(2024, 3, 31), 200),
            (date(2025, 3, 31), 300),
        ]

    def test_hides_values_first_known_after_as_of(self, session: Session):
        _put(session, period_end=date(2024, 3, 31), value=200, as_of=self.T1)
        _put(session, period_end=date(2025, 3, 31), value=300, as_of=self.T2)

        series = screener_schedules_series_as_of(
            session, isin="INE467B01029", line_item=_LINE, as_of=self.T1
        )

        assert [f.period_end for f in series] == [date(2024, 3, 31)]

    def test_restatement_visible_only_after_it_was_known(self, session: Session):
        _put(session, period_end=date(2024, 3, 31), value=200, as_of=self.T1)
        _put(session, period_end=date(2024, 3, 31), value=210, as_of=self.T2)

        before = screener_schedules_series_as_of(
            session, isin="INE467B01029", line_item=_LINE, as_of=self.T1
        )
        after = screener_schedules_series_as_of(
            session, isin="INE467B01029", line_item=_LINE, as_of=self.ASOF
        )

        assert [f.value for f in before] == [200]
        assert [f.value for f in after] == [210]

    def test_filters_isin_and_extractor(self, session: Session):
        _put(session, period_end=date(2024, 3, 31), value=200, as_of=self.T1)
        _put(session, isin="INE002A01012", period_end=date(2024, 3, 31), value=1, as_of=self.T1)
        _put(session, period_end=date(2023, 3, 31), value=2, as_of=self.T1, extracted_by="other")

        series = screener_schedules_series_as_of(
            session, isin="INE467B01029", line_item=_LINE, as_of=self.ASOF
        )

        assert [(f.period_end, f.value) for f in series] == [(date(2024, 3, 31), 200)]

    def test_empty_when_nothing_stored(self, session: Session):
        assert (
            screener_schedules_series_as_of(
                session, isin="INE467B01029", line_item=_LINE, as_of=self.ASOF
            )
            == []
        )

    def test_naive_as_of_rejected(self, session: Session):
        with pytest.raises(ValueError):
            screener_schedules_series_as_of(
                session, isin="INE467B01029", line_item=_LINE, as_of=datetime(2026, 9, 29)
            )
