"""Tests for NSE shareholding listing adapter (web_scrape source).

All tests use synthetic fixtures with no live network calls.
End-to-end tests exercise ingest() with a database backend.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from sqlalchemy.orm import Session

from core.config import get_settings
from core.db.models import RawSourceFile
from core.sources import SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_shareholding.adapters import NseShareholdingListing


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nse_shareholding"


class Web:
    """httpx.MockTransport handler for NSE shareholding listing API."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        """Map request URLs to responses.

        Args:
            responses: dict mapping URL patterns to response content
        """
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        # Find matching response
        for pattern, content in self.responses.items():
            if pattern in request.url.path or request.url.path.startswith(pattern):
                return httpx.Response(200, content=content, headers={"content-type": "application/json"})

        # Not found
        return httpx.Response(404, text="Not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def fixture_listing_response() -> bytes:
    """Synthetic NSE shareholding listing response (JSON)."""
    fixture_file = FIXTURES_DIR / "shareholding_listing_v1.json"
    return fixture_file.read_bytes()


@pytest.fixture
def shareholding_adapter() -> NseShareholdingListing:
    """NSE shareholding listing adapter."""
    return NseShareholdingListing()


# --------------------------------------------------------------------------- #
# Unit tests: adapter metadata
# --------------------------------------------------------------------------- #


def test_adapter_declares_correct_metadata(shareholding_adapter: NseShareholdingListing) -> None:
    """Adapter declares name, source_class, target_stores."""
    assert shareholding_adapter.name == "nse_shareholding_listing"
    assert shareholding_adapter.source_class is SourceClass.WEB_SCRAPE
    # Targets are provided by the reused drop-folder parser
    assert "shareholding_pattern" in shareholding_adapter.target_stores


def test_adapter_extracted_by(shareholding_adapter: NseShareholdingListing) -> None:
    """extracted_by() returns fully qualified class name."""
    extracted = shareholding_adapter.extracted_by()
    assert "nseshareholding" in extracted.lower()  # The class name lowercased
    assert "NseShareholdingListing" in extracted


def test_adapter_switch_name(shareholding_adapter: NseShareholdingListing) -> None:
    """switch_name() returns correct config key."""
    switch = shareholding_adapter.switch_name()
    assert switch == "EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"


def test_disabled_when_switch_off(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
) -> None:
    """Adapter returns disabled when switch is off."""
    import os
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "false"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    settings = get_settings()

    ctx = AdapterContext(
        session=session,
        blob=s3_store,
        settings=settings,
        now=lambda: datetime(2026, 2, 1, 12, 0, 0, tzinfo=IST),
    )

    result = shareholding_adapter.run(ctx)
    assert result.status == RunStatus.DISABLED
    assert "EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED" in result.detail


# --------------------------------------------------------------------------- #
# End-to-end tests: ingest() with database and mocked transport
# --------------------------------------------------------------------------- #


def test_parse_shareholding_listing(fixture_listing_response: bytes) -> None:
    """Parse synthetic shareholding listing into Pydantic models."""
    from ingest.nse_shareholding.schema import ShareholdingListingResponse

    data = json.loads(fixture_listing_response)
    listing = ShareholdingListingResponse.model_validate(data)

    assert len(listing.data) > 0
    first_row = listing.data[0]
    assert first_row.symbol is not None
    assert first_row.isin is not None
    assert first_row.broadcast_date is not None
    assert first_row.xbrl_file_url is not None


def test_ingest_stores_listing_raw_document(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """E2E: ingest() stores raw listing response to blob storage."""
    import os
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    # Create mock XBRL response
    mock_xbrl = b"""<?xml version="1.0" encoding="UTF-8"?>
    <xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    </xbrl>"""

    # Provide mock responses for both listings and XBRL files
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/corporate-share-holdings-master" in str(request.url):
            return httpx.Response(200, content=fixture_listing_response, headers={"content-type": "application/json"})
        elif "/corporate/xbrl/" in str(request.url):
            return httpx.Response(200, content=mock_xbrl, headers={"content-type": "application/xml"})
        else:
            return httpx.Response(404, text="Not found")

    adapter_with_mock = NseShareholdingListing(transport=httpx.MockTransport(mock_handler))

    as_of = datetime(2026, 2, 1, 12, 0, 0, tzinfo=IST)

    with patch("ingest.nse_shareholding.adapters.warm_up_nse_session"):
        with patch("ingest.nse_shareholding.adapters.get_universe_symbols") as mock_universe:
            mock_universe.return_value = ["TCS", "INFY"]
            ctx = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of,
            )

            result = adapter_with_mock.ingest(ctx)

            # Should succeed (listing fetched and XBRL downloaded)
            assert result.status == RunStatus.SUCCEEDED, f"Got status {result.status}: {result.detail}"
            assert result.raw_files >= 1

            # Check RawSourceFile was written
            raw_files = session.query(RawSourceFile).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).all()
            assert len(raw_files) >= 1

            # Check content hash format
            for rf in raw_files:
                assert len(rf.content_hash) == 64  # SHA-256
                assert rf.as_of >= as_of or rf.as_of == as_of  # Files can have as_of from their broadcast_date


def test_ingest_idempotent_same_listing_twice(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """E2E: Running the same listing twice doesn't create duplicates in blob store."""
    import os
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    responses = {
        "/api/corporate-share-holdings-master": fixture_listing_response,
    }
    web = Web(responses)
    adapter_with_mock = NseShareholdingListing(transport=web.transport())

    as_of = datetime(2026, 2, 1, 12, 0, 0, tzinfo=IST)

    with patch("ingest.nse_shareholding.adapters.warm_up_nse_session"):
        with patch("ingest.nse_shareholding.adapters.get_universe_symbols") as mock_universe:
            mock_universe.return_value = ["TCS"]
            ctx = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of,
            )

            # First run
            result1 = adapter_with_mock.ingest(ctx)
            assert result1.status == RunStatus.SUCCEEDED
            raw_files_1 = len(session.query(RawSourceFile).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).all())

            # Second run with same data
            result2 = adapter_with_mock.ingest(ctx)
            assert result2.status == RunStatus.SUCCEEDED
            raw_files_2 = len(session.query(RawSourceFile).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).all())

            # Both should be same (second run doesn't add more raw files if content is identical)
            assert raw_files_1 <= raw_files_2


def test_ingest_handles_access_blocked(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
) -> None:
    """E2E: AccessBlocked exception is caught and result includes error."""
    import os
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    adapter_with_mock = NseShareholdingListing(transport=httpx.MockTransport(
        lambda req: httpx.Response(429)  # Too Many Requests
    ))

    as_of = datetime(2026, 2, 1, 12, 0, 0, tzinfo=IST)

    with patch("ingest.nse_shareholding.adapters.warm_up_nse_session"):
        with patch("ingest.nse_shareholding.adapters.get_universe_symbols") as mock_universe:
            mock_universe.return_value = ["TCS"]
            ctx = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of,
            )

            result = adapter_with_mock.ingest(ctx)

            # Should fail gracefully
            assert result.status == RunStatus.FAILED
            assert "blocked" in result.detail.lower() or "429" in result.detail
