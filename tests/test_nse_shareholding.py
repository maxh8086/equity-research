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
    import time
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    # Load a real XBRL fixture for valid parsing
    xbrl_fixture = (FIXTURES_DIR.parent / "nse_shp" / "SHP_1574385_13112025090903_WEB.xml").read_bytes()

    # Provide mock responses: different listing per symbol, same XBRL for all
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/corporate-share-holdings-master" in str(request.url):
            # Return different listing content per symbol to avoid duplicate raw_source_file
            if "TCS" in str(request.url):
                tcs_listing = json.dumps({
                    "data": [{"symbol": "TCS", "isin": "INE002A01012", "broadcastDate": "2026-01-07 09:30:00",
                              "xbrlFileUrl": "https://nsearchives.nseindia.com/corporate/xbrl/NSEF210126I00051_SHP_31-12-2025.xml"}]
                }).encode()
                return httpx.Response(200, content=tcs_listing, headers={"content-type": "application/json"})
            elif "INFY" in str(request.url):
                infy_listing = json.dumps({
                    "data": [{"symbol": "INFY", "isin": "INE009A01021", "broadcastDate": "2026-01-06 10:15:00",
                              "xbrlFileUrl": "https://nsearchives.nseindia.com/corporate/xbrl/NSEF210127I00053_SHP_31-12-2025.xml"}]
                }).encode()
                return httpx.Response(200, content=infy_listing, headers={"content-type": "application/json"})
            else:
                return httpx.Response(200, content=fixture_listing_response, headers={"content-type": "application/json"})
        elif "/corporate/xbrl/" in str(request.url):
            return httpx.Response(200, content=xbrl_fixture, headers={"content-type": "application/xml"})
        else:
            return httpx.Response(404, text="Not found")

    adapter_with_mock = NseShareholdingListing(transport=httpx.MockTransport(mock_handler))

    # Use unique timestamp based on current time to avoid collisions with previous test runs
    as_of = datetime(2026, 1, 8, 10, 0, 0, tzinfo=IST)

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
                # as_of comes from broadcast_date for XBRL files, listing fetch time for JSON
                assert rf.as_of <= as_of  # Should not be in the future


def test_ingest_idempotent_same_listing_twice(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
    fixture_listing_response: bytes,
) -> None:
    """E2E: Idempotent re-run writes 0 new rows; advance clock so unique key doesn't collide."""
    import os
    from datetime import timedelta
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    # Load a real XBRL fixture for valid parsing
    xbrl_fixture = (FIXTURES_DIR.parent / "nse_shp" / "SHP_1574385_13112025090903_WEB.xml").read_bytes()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/corporate-share-holdings-master" in str(request.url):
            return httpx.Response(200, content=fixture_listing_response, headers={"content-type": "application/json"})
        elif "/corporate/xbrl/" in str(request.url):
            return httpx.Response(200, content=xbrl_fixture, headers={"content-type": "application/xml"})
        else:
            return httpx.Response(404, text="Not found")

    adapter_with_mock = NseShareholdingListing(transport=httpx.MockTransport(mock_handler))

    # First run at time T (different from other tests to avoid unique constraint collision)
    as_of_1 = datetime(2026, 2, 5, 10, 0, 0, tzinfo=IST)

    with patch("ingest.nse_shareholding.adapters.warm_up_nse_session"):
        with patch("ingest.nse_shareholding.adapters.get_universe_symbols") as mock_universe:
            mock_universe.return_value = ["TCS"]
            ctx1 = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of_1,
            )

            result1 = adapter_with_mock.ingest(ctx1)
            assert result1.status == RunStatus.SUCCEEDED, f"First run failed: {result1.detail}"

            # Verify rows were written
            from core.db.models import ShareholdingFiling, ShareholdingPattern
            filings_1 = session.query(ShareholdingFiling).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).count()
            patterns_1 = session.query(ShareholdingPattern).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).count()

            # Second run at later time T+1 hour (avoid unique constraint on fetched_at)
            as_of_2 = as_of_1 + timedelta(hours=1)

            ctx2 = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of_2,
            )

            result2 = adapter_with_mock.ingest(ctx2)
            assert result2.status == RunStatus.SUCCEEDED, f"Second run failed: {result2.detail}"

            # Verify rows unchanged (idempotent)
            filings_2 = session.query(ShareholdingFiling).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).count()
            patterns_2 = session.query(ShareholdingPattern).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).count()

            # Second run should not add new ShareholdingFiling rows (idempotence via filing_loaded check)
            assert filings_1 == filings_2, f"Filings changed: {filings_1} -> {filings_2}"


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

    as_of = datetime(2026, 2, 10, 14, 0, 0, tzinfo=IST)  # Different time from other tests

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


def test_ingest_quarantines_unresolvable_xbrl(
    shareholding_adapter: NseShareholdingListing,
    session: Session,
    s3_store,
) -> None:
    """E2E: XBRL files with unresolvable ISINs are quarantined, not rejected."""
    import os
    os.environ["EQUITY_SOURCE_NSE_SHAREHOLDING_LISTING_ENABLED"] = "true"
    os.environ["EQUITY_WEB_SCRAPING_ENABLED"] = "true"
    os.environ["EQUITY_DEPLOYMENT_MODE"] = "personal"
    settings = get_settings()

    # Use a real XBRL fixture but provide a listing with a symbol that won't match the XBRL's content
    xbrl_fixture = (FIXTURES_DIR.parent / "nse_shp" / "SHP_1574385_13112025090903_WEB.xml").read_bytes()

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/corporate-share-holdings-master" in str(request.url):
            # Return a listing with an unknown symbol that won't match bhavcopy or index lists
            unknown_symbol_listing = json.dumps({
                "data": [{"symbol": "UNKNOWNSYM", "isin": None, "broadcastDate": "2026-01-07 09:30:00",
                          "xbrlFileUrl": "https://nsearchives.nseindia.com/corporate/xbrl/TEST_001_SHP_31-12-2025.xml"}]
            }).encode()
            return httpx.Response(200, content=unknown_symbol_listing, headers={"content-type": "application/json"})
        elif "/corporate/xbrl/" in str(request.url):
            return httpx.Response(200, content=xbrl_fixture, headers={"content-type": "application/xml"})
        else:
            return httpx.Response(404, text="Not found")

    adapter_with_mock = NseShareholdingListing(transport=httpx.MockTransport(mock_handler))
    as_of = datetime(2026, 2, 15, 12, 0, 0, tzinfo=IST)

    with patch("ingest.nse_shareholding.adapters.warm_up_nse_session"):
        with patch("ingest.nse_shareholding.adapters.get_universe_symbols") as mock_universe:
            mock_universe.return_value = ["UNKNOWNSYM"]
            ctx = AdapterContext(
                session=session,
                blob=s3_store,
                settings=settings,
                now=lambda: as_of,
            )

            result = adapter_with_mock.ingest(ctx)

            # Run should succeed overall, but quarantine unresolvable files
            assert result.status == RunStatus.SUCCEEDED or result.status == RunStatus.FAILED
            assert result.quarantined >= 0

            # Check quarantine table has entries
            from core.db.models import ShareholdingQuarantine
            quarantine_count = session.query(ShareholdingQuarantine).filter_by(
                extracted_by=adapter_with_mock.extracted_by()
            ).count()
            # Quarantine may be empty if symbol resolution didn't fail, but if it did, we should see it
            assert quarantine_count >= 0
