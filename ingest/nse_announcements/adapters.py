"""NSE corporate announcements adapter (web_scrape source).

Fetches announcements from NSE's /api/corporate-announcements?index=equities endpoint.
Stores raw JSON response to blob before parsing. Classifies announcements using hardcoded
keyword rules and event-date extraction. Resolves ISIN from symbol via dated symbol_to_isin_as_of lookup.
Quarantines rows that don't resolve to ISIN.

Unclassifiable announcements (no type matched or no event_date extracted) are left unwritten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.exc import SQLAlchemyError

from core.db.models import ScheduledEvent, ScheduledEventSeverity
from core.db.pit import scheduled_event_exists_as_of, symbol_to_isin_as_of
from core.sources import SourceClass
from core.timezones import IST, require_aware
from ingest.base import Adapter, AdapterContext, RawDocument, RunResult, RunStatus
from ingest.http import AccessBlocked, PoliteClient
from ingest.nse_announcements.classifier import classify_announcement
from ingest.nse_announcements.schema import AnnouncementRow, AnnouncementsListingResponse
from ingest.nse_session import create_nse_client, warm_up_nse_session

TARGET_STORES = ("scheduled_event",)
RULE_VERSION = "announcements_ingest_v1"


@dataclass
class Tally:
    """Statistics from a run."""

    raw_files: int = 0
    rows_parsed: int = 0
    rows_written: int = 0
    rows_unclassified: int = 0
    rows_isin_unresolved: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter_name: str) -> RunResult:
        """Convert tally to RunResult."""
        detail_parts = []
        if self.rows_parsed:
            detail_parts.append(f"{self.rows_parsed} rows parsed")
        if self.rows_written:
            detail_parts.append(f"{self.rows_written} rows written")
        if self.rows_unclassified:
            detail_parts.append(f"{self.rows_unclassified} rows unclassified")
        if self.rows_isin_unresolved:
            detail_parts.append(f"{self.rows_isin_unresolved} rows ISIN unresolved")
        if self.problems:
            detail_parts.append("; ".join(self.problems))

        detail = ", ".join(detail_parts) if detail_parts else "no announcements"
        failed = bool(self.problems)

        return RunResult(
            adapter_name,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.rows_written,
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


class NseAnnouncements(Adapter):
    """Fetch announcements from NSE's corporate announcements API.

    Reads /api/corporate-announcements?index=equities, stores raw JSON response to blob,
    validates with strict Pydantic, classifies using hardcoded keyword rules and event-date
    extraction, resolves ISIN from symbol using dated lookup, and writes ScheduledEvent rows.

    Unclassifiable announcements (no type matched or no event_date extracted) are stored raw
    but not written to any table. Rows that do not resolve to ISIN are quarantined.

    If NSE blocks (401/403/429/451), raises AccessBlocked and falls back to drop-folder adapter.
    Never retries on overload (502/503/504); that is the drop-folder's job.
    """

    name = "nse_announcements"
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
        """Fetch announcements, store raw, parse, classify, resolve ISIN, and write."""
        tally = Tally()
        url = ctx.settings.nse_announcements_api_url
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

                # Fetch announcements
                try:
                    response = http.get(url)
                except AccessBlocked as exc:
                    tally.problems.append(f"announcements API blocked: {exc}")
                    return tally.result(self.name)
                except httpx.HTTPError as exc:
                    tally.problems.append(f"announcements API error: {type(exc).__name__}: {exc}")
                    return tally.result(self.name)

                # Store raw response to blob
                try:
                    doc = self.store_raw(
                        ctx,
                        data=response.content,
                        source_url=url,
                        as_of=as_of,
                        media_type=response.headers.get("content-type", "application/json"),
                    )
                    tally.raw_files += 1
                except (IOError, OSError) as exc:
                    tally.problems.append(f"blob store error: {type(exc).__name__}: {exc}")
                    return tally.result(self.name)

                # Parse and validate response
                try:
                    data = response.json()
                    listing = AnnouncementsListingResponse.model_validate(data)
                except ValueError as exc:
                    tally.problems.append(f"JSON validation error: {exc}")
                    return tally.result(self.name)

                # Process each announcement
                prov = _provenance(self, doc)
                events_to_write = []

                for row_data in listing.data:
                    tally.rows_parsed += 1

                    # Classify the announcement (may return None if unclassifiable)
                    classification = classify_announcement(row_data)
                    if classification is None:
                        # Unclassifiable: no type matched or no date extracted
                        tally.rows_unclassified += 1
                        continue

                    # Resolve ISIN from symbol at the announced_on date
                    isin = symbol_to_isin_as_of(
                        ctx.session,
                        symbol=row_data.symbol,
                        on=row_data.announced_on.date(),
                        as_of=as_of,
                    )

                    if isin is None:
                        # ISIN not resolved: quarantine for review
                        tally.rows_isin_unresolved += 1
                        continue

                    # Check if event already exists (idempotence)
                    event_type = classification["event_type"]
                    event_date = classification["event_date"]
                    if scheduled_event_exists_as_of(
                        ctx.session,
                        isin=isin,
                        event_type=event_type,
                        event_date=event_date,
                        as_of=as_of,
                    ):
                        # Row already written in a previous run; skip
                        continue

                    # Create ScheduledEvent with resolved ISIN
                    event = ScheduledEvent(
                        isin=isin,
                        event_type=event_type,
                        severity=ScheduledEventSeverity.MEDIUM,  # Computed by code later
                        event_date=event_date,
                        description=row_data.description or "",
                        **prov,
                    )
                    events_to_write.append(event)

                # Write all events atomically
                if events_to_write:
                    try:
                        ctx.session.add_all(events_to_write)
                        ctx.session.flush()
                        tally.rows_written = len(events_to_write)
                    except SQLAlchemyError as exc:
                        tally.problems.append(f"database write error: {type(exc).__name__}: {exc}")
                        return tally.result(self.name)

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
        url = ctx.settings.nse_announcements_api_url

        with self._client(ctx) as http:
            # Warm up session first
            warm_up_nse_session(http)

            # Fetch and validate schema
            response = http.get(url)
            data = response.json()
            AnnouncementsListingResponse.model_validate(data)
