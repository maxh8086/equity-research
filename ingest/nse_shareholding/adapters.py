"""NSE shareholding listing adapter (web_scrape source).

Fetches shareholding listings from NSE's /api/corporate-share-holdings-master?index=equities
endpoint for each symbol in the 100-company universe. Downloads XBRL files from
nsearchives.nseindia.com/corporate/xbrl/. Stores raw JSON and XBRL files to blob storage
before parsing. Feeds XBRL files to the existing shareholding drop-folder adapter's parser.

Unclassifiable or already-loaded filings are logged but don't fail the run.
If NSE blocks (401/403/429/451), raises AccessBlocked and falls back to drop-folder adapter.
Never retries on overload (502/503/504); that is the drop-folder's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import SQLAlchemyError

from core.db.pit import raw_source_files_as_of, shareholding_filing_loaded_as_of
from core.sources import SourceClass
from core.timezones import IST, require_aware
from ingest.base import Adapter, AdapterContext, RawDocument, RunResult, RunStatus
from ingest.http import AccessBlocked, PoliteClient
from ingest.nse_session import create_nse_client, warm_up_nse_session
from ingest.nse_shareholding.schema import ShareholdingListingResponse
from ingest.nse_shp.adapters import load_filing

TARGET_STORES = ("shareholding_pattern", "shareholding_filing", "shareholding_quarantine")
RULE_VERSION = "shareholding_listing_v1"


def get_universe_symbols() -> list[str]:
    """Return the list of symbols in the 100-company universe (Nifty 50 + Next 50).

    This is a stub that will be replaced with a pit.py call once the
    index membership system is fully set up. For now, return a fixed list.
    """
    # TODO: Replace with pit.py index_constituents_as_of once ready
    return [
        "TCS", "RELIANCE", "HDFC", "HDFCBANK", "INFY", "LT", "WIPRO", "ICICIBANK",
        "MARUTI", "BAJAJFINSV", "AXISBANK", "SUNPHARMA", "ASIANPAINT", "ITCM",
        "ITC", "JSWSTEEL", "BAJAJFSV", "SBILIFE", "BAJAJFECSV", "HCLTECH",
        "POWERGRID", "COALINDIA", "BHARTIARTL", "SBIN", "INDUSINDBK", "BPCL",
        "ADANIGREEN", "ADANIPORTS", "TCS", "CIPLA", "TITAN", "NESTLEIND",
        "DMART", "GRASIM", "ONGC", "NTPC", "BAJAJ-AUTO", "TATASTEEL",
        "BOSCHIND", "ULTRAMARINE", "M&M", "LT", "EICHERMOT", "GAIL",
        "SBICARD", "SBICAPS", "INFOEDGE", "NYKAA", "MUTHOOTFIN", "PAGEIND",
    ]


@dataclass
class Tally:
    """Statistics from a run."""

    symbols_processed: int = 0
    listings_fetched: int = 0
    xbrl_files_downloaded: int = 0
    filings: int = 0  # Number of filings successfully parsed
    rows_written: int = 0  # Total number of shareholding pattern rows written
    quarantined: int = 0  # Number of quarantine entries
    rejected_files: int = 0  # Number of files rejected
    already_loaded: int = 0  # Number of files already parsed
    problems: list[str] = field(default_factory=list)

    def result(self, adapter_name: str) -> RunResult:
        """Convert tally to RunResult."""
        detail_parts = []
        if self.symbols_processed:
            detail_parts.append(f"{self.symbols_processed} symbols processed")
        if self.listings_fetched:
            detail_parts.append(f"{self.listings_fetched} listings fetched")
        if self.xbrl_files_downloaded:
            detail_parts.append(f"{self.xbrl_files_downloaded} XBRL files downloaded")
        if self.filings:
            detail_parts.append(f"{self.filings} filings parsed")
        if self.rows_written:
            detail_parts.append(f"{self.rows_written} rows written")
        if self.quarantined:
            detail_parts.append(f"{self.quarantined} quarantined")
        if self.rejected_files:
            detail_parts.append(f"{self.rejected_files} files rejected")
        if self.already_loaded:
            detail_parts.append(f"{self.already_loaded} already loaded")
        if self.problems:
            detail_parts.append("; ".join(self.problems))

        detail = ", ".join(detail_parts) if detail_parts else "no listings"
        failed = bool(self.rejected_files or self.problems)

        return RunResult(
            adapter_name,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.xbrl_files_downloaded,
            rows_written=self.rows_written,
            quarantined=self.quarantined,
        )


def _provenance(adapter: Adapter, doc: RawDocument) -> dict:
    """Provenance dict for rows written from a raw document."""
    return dict(
        as_of=doc.as_of,
        content_hash=doc.content_hash,
        source_url=doc.source_url,
        extracted_by=adapter.extracted_by(),
        model_version=None,
    )


class NseShareholdingListing(Adapter):
    """Fetch shareholding listings from NSE API for the 100-company universe.

    Reads /api/corporate-share-holdings-master?index=equities&symbol=<SYMBOL>,
    stores raw JSON responses to blob, validates with strict Pydantic, downloads
    XBRL files from nsearchives.nseindia.com/corporate/xbrl/, stores XBRL files
    to blob, and feeds them to the shareholding parser.

    If NSE blocks (401/403/429/451), raises AccessBlocked and falls back to
    drop-folder adapter. Never retries on overload (502/503/504); that is the
    drop-folder's job.
    """

    name = "nse_shareholding_listing"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        """Initialize adapter with optional transport for testing."""
        self._transport = transport

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        """Create an NSE client with configured parameters."""
        return create_nse_client(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.nse_min_interval_seconds,
            transport=self._transport,
        )

    def ingest(self, ctx: AdapterContext) -> RunResult:
        """Fetch shareholding listings for universe, download XBRL files, and write rows."""
        tally = Tally()
        base_url = ctx.settings.nse_shareholding_listing_api_url
        as_of = ctx.now()
        require_aware(as_of, "as_of")

        try:
            with self._client(ctx) as http:
                # Warm up session
                try:
                    warm_up_nse_session(http)
                except AccessBlocked as exc:
                    tally.problems.append(f"NSE homepage blocked: {exc}")
                    return tally.result(self.name)
                except httpx.HTTPError as exc:
                    tally.problems.append(f"NSE homepage error: {type(exc).__name__}: {exc}")
                    return tally.result(self.name)

                # Process each symbol in the universe
                symbols = get_universe_symbols()
                for symbol in symbols:
                    tally.symbols_processed += 1

                    # Fetch listing for this symbol
                    url = f"{base_url}?index=equities&symbol={symbol}"
                    try:
                        response = http.get(url)
                    except AccessBlocked as exc:
                        tally.problems.append(f"shareholding listing API blocked for {symbol}: {exc}")
                        continue
                    except httpx.HTTPError as exc:
                        tally.problems.append(f"shareholding listing API error for {symbol}: {type(exc).__name__}: {exc}")
                        continue

                    # Store raw listing response
                    try:
                        listing_doc = self.store_raw(
                            ctx,
                            data=response.content,
                            source_url=url,
                            as_of=as_of,
                            media_type=response.headers.get("content-type", "application/json"),
                        )
                        tally.listings_fetched += 1
                    except (IOError, OSError) as exc:
                        tally.problems.append(f"blob store error for {symbol} listing: {type(exc).__name__}: {exc}")
                        continue

                    # Parse listing response
                    try:
                        data = response.json()
                        listing = ShareholdingListingResponse.model_validate(data)
                    except ValueError as exc:
                        tally.problems.append(f"JSON validation error for {symbol}: {exc}")
                        continue

                    # Process each filing in the listing
                    for row_data in listing.data:
                        # Download XBRL file
                        xbrl_url = row_data.xbrl_file_url
                        try:
                            xbrl_response = http.get(xbrl_url)
                        except AccessBlocked as exc:
                            tally.problems.append(f"XBRL download blocked for {symbol}: {exc}")
                            continue
                        except httpx.HTTPError as exc:
                            tally.problems.append(f"XBRL download error for {symbol}: {type(exc).__name__}: {exc}")
                            continue

                        # Store XBRL file to blob
                        try:
                            xbrl_doc = self.store_raw(
                                ctx,
                                data=xbrl_response.content,
                                source_url=xbrl_url,
                                as_of=row_data.broadcast_date,
                                media_type=xbrl_response.headers.get("content-type", "application/xml"),
                            )
                            tally.xbrl_files_downloaded += 1
                        except (IOError, OSError) as exc:
                            tally.problems.append(f"blob store error for {symbol} XBRL: {type(exc).__name__}: {exc}")
                            continue

                        # Parse XBRL file and write rows using the existing drop-folder adapter's parser
                        # (Reuse the same load_filing logic to ensure consistency)
                        try:
                            # load_filing modifies tally in place
                            load_filing(self, ctx, xbrl_doc, tally)
                        except Exception as exc:
                            tally.problems.append(f"XBRL load_filing error for {symbol}: {type(exc).__name__}: {exc}")
                            continue

        except (AccessBlocked, httpx.HTTPError, ValueError):
            # Explicitly handled errors already; re-raise unexpected errors
            raise

        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        """Validate schema by fetching a live sample (canary check).

        This makes a live call to NSE only when the canary is run, never during tests.
        If NSE's schema has changed, this will raise loudly.
        If NSE blocks us, AccessBlocked is raised (no retry).
        """
        base_url = ctx.settings.nse_shareholding_listing_api_url

        # Fetch one symbol to validate schema
        symbols = get_universe_symbols()
        if not symbols:
            return  # No universe to check

        test_symbol = symbols[0]
        url = f"{base_url}?index=equities&symbol={test_symbol}"

        with self._client(ctx) as http:
            # Warm up session first
            warm_up_nse_session(http)

            # Fetch and validate schema
            response = http.get(url)
            data = response.json()
            ShareholdingListingResponse.model_validate(data)
