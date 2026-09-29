"""Tests for NSE announcements adapter (web_scrape source).

All tests use synthetic fixtures with no live network calls.
End-to-end tests exercise ingest() with a database backend.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db.models import RawSourceFile, ScheduledEvent, ScheduledEventSeverity, ScheduledEventType
from core.sources import SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_announcements.adapters import NseAnnouncements


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nse_announcements"


class Web:
    """httpx.MockTransport handler for NSE announcements API."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=self.data, headers={"content-type": "application/json"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def fixture_listing_response() -> bytes:
    """Synthetic NSE corporate announcements listing (JSON)."""
    fixture_file = FIXTURES_DIR / "announcements_listing_v1.json"
    return fixture_file.read_bytes()


@pytest.fixture
def announcements_adapter() -> NseAnnouncements:
    """NSE announcements adapter."""
    return NseAnnouncements()


# --------------------------------------------------------------------------- #
# Unit tests: schema, classification, metadata
# --------------------------------------------------------------------------- #


def test_adapter_declares_correct_metadata(announcements_adapter: NseAnnouncements) -> None:
    """Adapter declares name, source_class, target_stores."""
    assert announcements_adapter.name == "nse_announcements"
    assert announcements_adapter.source_class is SourceClass.WEB_SCRAPE
    assert "scheduled_event" in announcements_adapter.target_stores


def test_adapter_extracted_by(announcements_adapter: NseAnnouncements) -> None:
    """extracted_by() returns fully qualified class name."""
    extracted = announcements_adapter.extracted_by()
    assert "nse_announcements" in extracted.lower()
    assert "NseAnnouncements" in extracted


def test_adapter_switch_name(announcements_adapter: NseAnnouncements) -> None:
    """switch_name() returns correct config key."""
    switch = announcements_adapter.switch_name()
    assert switch == "EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED"


def test_parse_announcements_listing(fixture_listing_response: bytes) -> None:
    """Parse synthetic announcements listing into Pydantic models."""
    from ingest.nse_announcements.schema import AnnouncementRow

    data = json.loads(fixture_listing_response)
    rows = [AnnouncementRow.model_validate(item) for item in data.get("data", [])]

    assert len(rows) > 0
    first_row = rows[0]
    assert first_row.subject is not None
    assert first_row.announced_on is not None
    assert isinstance(first_row.announced_on, datetime)
    assert first_row.announced_on.tzinfo is IST


def test_classify_board_meeting_with_date() -> None:
    """Classification: board meeting announcement with parseable date."""
    from ingest.nse_announcements.schema import AnnouncementRow
    from ingest.nse_announcements.classifier import classify_announcement

    announcement = AnnouncementRow(
        subject="Board Meeting to be held on 15-Feb-2026",
        description="Meeting to consider quarterly results",
        announced_on=datetime(2026, 1, 10, 10, 30, 0, tzinfo=IST),
        isin="INE002A01012",
        symbol="TCS",
    )

    classification = classify_announcement(announcement)
    assert classification is not None
    assert classification["event_type"] == ScheduledEventType.BOARD_MEETING
    assert classification["event_date"] == date(2026, 2, 15)


def test_classify_results_with_date() -> None:
    """Classification: results announcement with parseable quarter date."""
    from ingest.nse_announcements.schema import AnnouncementRow
    from ingest.nse_announcements.classifier import classify_announcement

    announcement = AnnouncementRow(
        subject="Results for Quarter Ended 31-Dec-2025",
        announced_on=datetime(2025, 12, 31, 15, 0, 0, tzinfo=IST),
        isin="INE002A01012",
        symbol="TCS",
    )

    classification = classify_announcement(announcement)
    assert classification is not None
    assert classification["event_type"] == ScheduledEventType.RESULTS
    assert classification["event_date"] == date(2025, 12, 31)


def test_unclassifiable_no_parseable_date() -> None:
    """Classification: no date parsed returns None (will not be written)."""
    from ingest.nse_announcements.schema import AnnouncementRow
    from ingest.nse_announcements.classifier import classify_announcement

    announcement = AnnouncementRow(
        subject="Board Announcement",
        description="No date mentioned anywhere",
        announced_on=datetime(2026, 1, 15, 10, 30, 0, tzinfo=IST),
        isin="INE002A01012",
        symbol="TCS",
    )

    classification = classify_announcement(announcement)
    assert classification is None  # No date = unclassifiable


def test_disabled_when_switch_off(
    announcements_adapter: NseAnnouncements,
    session: Session,
    s3_store,
) -> None:
    """Adapter returns disabled when switch is off."""
    import os
    os.environ["EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED"] = "false"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    settings = get_settings()

    ctx = AdapterContext(
        session=session,
        blob=s3_store,
        settings=settings,
        now=lambda: datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST),
    )

    result = announcements_adapter.run(ctx)
    assert result.status == RunStatus.DISABLED
    assert "EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED" in result.detail


# --------------------------------------------------------------------------- #
# End-to-end tests: ingest() with database and mocked transport
# --------------------------------------------------------------------------- #


def test_ingest_writes_scheduled_events_to_db(
    announcements_adapter: NseAnnouncements,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """E2E: ingest() parses fixture, resolves ISINs, writes ScheduledEvent rows."""
    import os
    os.environ["EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    web = Web(fixture_listing_response)
    adapter_with_mock = NseAnnouncements(transport=web.transport())

    as_of = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)

    with patch("ingest.nse_announcements.adapters.symbol_to_isin_as_of") as mock_symbol_to_isin:
        mock_symbol_to_isin.return_value = "INE002A01012"

        with patch("ingest.nse_announcements.adapters.warm_up_nse_session"):
            ctx = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of,
            )

            result = adapter_with_mock.ingest(ctx)

            # Should succeed
            assert result.status == RunStatus.SUCCEEDED
            assert result.raw_files == 1
            # 3 rows should be written (fixture has 5, but 2 are unclassifiable)
            # Row 1: Board Meeting - has date - classified
            # Row 2: Results - has date - classified
            # Row 3: AGM - has date - classified
            # Row 4: Buyback - no date - NOT classified
            # Row 5: Misc - no keyword - NOT classified
            assert result.rows_written == 3

            # Check RawSourceFile was written
            raw_files = session.query(RawSourceFile).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).all()
            assert len(raw_files) == 1
            assert raw_files[0].as_of == as_of
            assert len(raw_files[0].content_hash) == 64  # SHA-256

            # Check ScheduledEvent rows
            events = session.query(ScheduledEvent).all()
            assert len(events) == 3

            # Each should have provenance
            for event in events:
                assert event.as_of == as_of
                assert len(event.content_hash) == 64
                assert event.source_url is not None
                assert event.extracted_by == adapter_with_mock.extracted_by()
                assert event.model_version is None
                assert event.severity is not None
                assert event.event_date is not None


def test_ingest_idempotent_no_duplicate_rows(
    announcements_adapter: NseAnnouncements,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """E2E: Running ingest twice with same data doesn't duplicate rows.

    Second run has advanced as_of (later fetched_at for raw file).
    """
    import os
    from datetime import timedelta
    os.environ["EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    web = Web(fixture_listing_response)
    adapter_with_mock = NseAnnouncements(transport=web.transport())

    as_of_1 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST)
    as_of_2 = as_of_1 + timedelta(hours=1)  # Advance clock for second run

    with patch("ingest.nse_announcements.adapters.symbol_to_isin_as_of") as mock_symbol_to_isin:
        mock_symbol_to_isin.return_value = "INE002A01012"

        with patch("ingest.nse_announcements.adapters.warm_up_nse_session"):
            # First run
            ctx1 = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of_1,
            )
            result1 = adapter_with_mock.ingest(ctx1)
            assert result1.status == RunStatus.SUCCEEDED
            assert result1.rows_written == 3
            assert result1.raw_files == 1

            # Second run with advanced clock (later fetched_at)
            ctx2 = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of_2,
            )
            result2 = adapter_with_mock.ingest(ctx2)
            assert result2.status == RunStatus.SUCCEEDED
            # No new rows should be written (idempotence check enforced)
            assert result2.rows_written == 0
            assert result2.raw_files == 1  # New raw file with later fetched_at

            # Verify total ScheduledEvent count didn't increase
            events = session.query(ScheduledEvent).all()
            assert len(events) == 3  # Still 3, not 6


def test_store_raw_saves_to_blob(
    announcements_adapter: NseAnnouncements,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """Store raw response to blob storage."""
    import os
    os.environ["EQUITY_SOURCE_NSE_ANNOUNCEMENTS_ENABLED"] = "true"
    settings = get_settings()

    ctx = AdapterContext(
        session=session,
        blob=s3_store,
        settings=settings,
        now=lambda: datetime(2026, 1, 15, 12, 0, 0, tzinfo=IST),
    )

    doc = announcements_adapter.store_raw(
        ctx,
        data=fixture_listing_response,
        source_url="https://www.nseindia.com/api/corporate-announcements?index=equities",
        as_of=datetime(2026, 1, 15, 11, 0, 0, tzinfo=IST),
        media_type="application/json",
    )

    assert doc.content_hash is not None
    assert len(doc.content_hash) == 64  # SHA-256 hex

    # Verify retrieval from blob
    stored = s3_store.get(doc.content_hash)
    assert stored == fixture_listing_response
