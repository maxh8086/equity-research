"""Test for pit.py scheduled_event_exists_as_of function."""

from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session

from core.db.models import ScheduledEvent, ScheduledEventSeverity, ScheduledEventType
from core.db.pit import scheduled_event_exists_as_of
from core.timezones import IST

pytestmark = pytest.mark.db


def test_scheduled_event_exists_as_of_returns_true_when_row_exists(session: Session) -> None:
    """Check returns True when event exists with matching (isin, event_type, event_date)."""
    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    event = ScheduledEvent(
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        severity=ScheduledEventSeverity.MEDIUM,
        event_date=date(2026, 2, 15),
        description="Board meeting",
        as_of=as_of,
        content_hash="a" * 64,
        source_url="https://example.com",
        extracted_by="test.adapter",
        model_version=None,
    )
    session.add(event)
    session.flush()

    result = scheduled_event_exists_as_of(
        session,
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        event_date=date(2026, 2, 15),
        as_of=as_of,
    )
    assert result is True


def test_scheduled_event_exists_as_of_returns_false_when_row_not_exists(session: Session) -> None:
    """Check returns False when no matching event exists."""
    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    result = scheduled_event_exists_as_of(
        session,
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        event_date=date(2026, 2, 15),
        as_of=as_of,
    )
    assert result is False


def test_scheduled_event_exists_as_of_distinguishes_by_isin(session: Session) -> None:
    """Check distinguishes rows by ISIN."""
    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    event = ScheduledEvent(
        isin="INE002A01012",  # TCS
        event_type=ScheduledEventType.BOARD_MEETING,
        severity=ScheduledEventSeverity.MEDIUM,
        event_date=date(2026, 2, 15),
        description="Board meeting",
        as_of=as_of,
        content_hash="a" * 64,
        source_url="https://example.com",
        extracted_by="test.adapter",
        model_version=None,
    )
    session.add(event)
    session.flush()

    # Same event_type and event_date, but different ISIN
    result = scheduled_event_exists_as_of(
        session,
        isin="INE002A01018",  # RELIANCE
        event_type=ScheduledEventType.BOARD_MEETING,
        event_date=date(2026, 2, 15),
        as_of=as_of,
    )
    assert result is False


def test_scheduled_event_exists_as_of_distinguishes_by_event_type(session: Session) -> None:
    """Check distinguishes rows by event_type."""
    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    event = ScheduledEvent(
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        severity=ScheduledEventSeverity.MEDIUM,
        event_date=date(2026, 2, 15),
        description="Board meeting",
        as_of=as_of,
        content_hash="b" * 64,
        source_url="https://example.com",
        extracted_by="test.adapter",
        model_version=None,
    )
    session.add(event)
    session.flush()

    # Same ISIN and event_date, but different event_type
    result = scheduled_event_exists_as_of(
        session,
        isin="INE002A01012",
        event_type=ScheduledEventType.RESULTS,
        event_date=date(2026, 2, 15),
        as_of=as_of,
    )
    assert result is False


def test_scheduled_event_exists_as_of_distinguishes_by_event_date(session: Session) -> None:
    """Check distinguishes rows by event_date."""
    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    event = ScheduledEvent(
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        severity=ScheduledEventSeverity.MEDIUM,
        event_date=date(2026, 2, 15),
        description="Board meeting",
        as_of=as_of,
        content_hash="c" * 64,
        source_url="https://example.com",
        extracted_by="test.adapter",
        model_version=None,
    )
    session.add(event)
    session.flush()

    # Same ISIN and event_type, but different event_date
    result = scheduled_event_exists_as_of(
        session,
        isin="INE002A01012",
        event_type=ScheduledEventType.BOARD_MEETING,
        event_date=date(2026, 3, 15),
        as_of=as_of,
    )
    assert result is False
