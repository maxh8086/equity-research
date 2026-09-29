"""Live NSE financial-results listing adapter (Session 4): a web_scrape source.

For each symbol in the universe (every constituent list public now) and each
configured period, it reads NSE's public results listing, then downloads each
filing's XBRL attachment from nsearchives.nseindia.com/corporate/xbrl/. Each
file is stored raw (`raw_source_file`, `as_of` = the listing's `exchdisstime`,
never the fetch time) before it is parsed, and is then handed to the same
`load_filing` the hand-drop adapter uses, so facts, filings and quarantine rows
are produced exactly as for a dropped file. The parser and its mapping are not
touched here: pre-2020 taxonomies stay quarantined as `unsupported_taxonomy`.

Idempotent: a file already stored from the same URL and publication time is
re-read from blob storage, not downloaded or recorded again, and `load_filing`
skips what is already loaded under the current rule version. A rerun therefore
adds no raw row, no fact and no filing, and retries only files that were
quarantined (for example an ISIN that has since become resolvable).

Polite and honest: one identified session, throttled, shared with the other NSE
adapters (`ingest/nse_session`). Any block (401/403/429/451) stops the whole
run; nothing is retried, rotated or worked around. The fallback is the drop
folder (`nse_xbrl_results_drop`). Not yet run against the live site.
"""

import dataclasses
import json
from collections.abc import Sequence

import httpx
from pydantic import ValidationError

from core.db.pit import index_universe_symbols_as_of, raw_source_file_by_url_as_of, raw_source_files_as_of
from core.sources import SourceClass
from core.timezones import require_aware
from ingest.base import Adapter, AdapterContext, RawDocument, RunResult
from ingest.http import AccessBlocked, PoliteClient
from ingest.nse_results_listing.schema import ResultsListingRow
from ingest.nse_session import create_nse_client, warm_up_nse_session
from ingest.nse_xbrl.adapters import TARGET_STORES, Tally, load_filing


class ListingShapeError(ValueError):
    """The listing is not the JSON list of rows this adapter understands."""


def parse_listing(content: bytes) -> list[ResultsListingRow]:
    """Validate a listing response. Raises ListingShapeError (a ValueError) on any drift."""
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise ListingShapeError(f"listing is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ListingShapeError(f"listing is a {type(payload).__name__}, not a list")
    try:
        return [ResultsListingRow.model_validate(item) for item in payload]
    except ValidationError as exc:
        raise ListingShapeError(f"listing row failed validation: {exc}") from exc


class NseResultsListing(Adapter):
    """Results XBRL fetched from NSE's financial-results listing. Off unless the switch and web scraping are on."""

    name = "nse_results_listing"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def __init__(
        self, transport: httpx.BaseTransport | None = None, symbols: Sequence[str] | None = None
    ) -> None:
        """`transport` is for tests. `symbols` overrides the universe, for a targeted run."""
        self._transport = transport
        self._symbols = list(symbols) if symbols is not None else None

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return create_nse_client(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.nse_min_interval_seconds,
            transport=self._transport,
        )

    def _universe(self, ctx: AdapterContext) -> list[str]:
        if self._symbols is not None:
            return self._symbols
        return index_universe_symbols_as_of(ctx.session, as_of=require_aware(ctx.now(), "now()"))

    @staticmethod
    def _listing_params(symbol: str, period: str) -> dict[str, str]:
        return {"index": "equities", "symbol": symbol, "period": period}

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        now = require_aware(ctx.now(), "now()")
        symbols = self._universe(ctx)
        if not symbols:
            return dataclasses.replace(tally.result(self.name), detail="no symbols in the universe; nothing requested")
        url = ctx.settings.nse_results_listing_api_url
        listings = skipped = 0
        with self._client(ctx) as http:
            try:
                warm_up_nse_session(http)
            except AccessBlocked as exc:
                tally.problems.append(f"NSE blocked the session warm-up: {exc}")
                return tally.result(self.name)
            except httpx.HTTPError as exc:
                tally.problems.append(f"NSE session warm-up failed: {type(exc).__name__}: {exc}")
                return tally.result(self.name)
            try:
                for symbol in symbols:
                    for period in ctx.settings.nse_results_listing_periods:
                        listings += 1
                        rows = self._fetch_listing(ctx, http, url, symbol, period, tally)
                        for row in rows:
                            if not row.has_xbrl():
                                skipped += 1
                                continue
                            self._load_row(ctx, http, row, now, tally)
            except AccessBlocked as exc:
                tally.problems.append(f"NSE blocked the run, stopping: {exc}")
        result = tally.result(self.name)
        prefix = f"{len(symbols)} symbols, {listings} listings, {skipped} filings without XBRL; "
        return dataclasses.replace(result, detail=prefix + result.detail)

    def _fetch_listing(
        self, ctx: AdapterContext, http: PoliteClient, url: str, symbol: str, period: str, tally: Tally
    ) -> list[ResultsListingRow]:
        """One symbol and period. A block propagates and stops the run; other failures skip this listing."""
        try:
            response = http.get(url, params=self._listing_params(symbol, period))
        except httpx.HTTPError as exc:
            tally.problems.append(f"listing for {symbol} {period} failed: {type(exc).__name__}: {exc}")
            return []
        self.store_raw(
            ctx, data=response.content, source_url=str(response.request.url), as_of=ctx.now(),
            media_type=response.headers.get("content-type", "application/json"),
        )  # fmt: skip
        try:
            return parse_listing(response.content)
        except ListingShapeError as exc:
            tally.problems.append(f"listing for {symbol} {period} changed shape: {exc}")
            return []

    def _load_row(
        self, ctx: AdapterContext, http: PoliteClient, row: ResultsListingRow, now, tally: Tally
    ) -> None:
        if row.exchdisstime > now:
            tally.problems.append(
                f"{row.symbol} {row.xbrl}: disseminated {row.exchdisstime}, after fetch time {now}; not stored"
            )
            return
        stored = raw_source_file_by_url_as_of(
            ctx.session, source_url=row.xbrl, file_as_of=row.exchdisstime, extracted_by=self.extracted_by(), as_of=now
        )
        if stored is not None:
            doc = RawDocument(
                data=ctx.blob.get(stored.content_hash), content_hash=stored.content_hash, source_url=stored.source_url,
                as_of=stored.as_of, fetched_at=stored.fetched_at, media_type=stored.media_type,
            )  # fmt: skip
        else:
            try:
                response = http.get(row.xbrl)
            except httpx.HTTPError as exc:
                tally.problems.append(f"{row.symbol} {row.xbrl}: download failed: {type(exc).__name__}: {exc}")
                return
            doc = self.store_raw(
                ctx, data=response.content, source_url=row.xbrl, as_of=row.exchdisstime,
                media_type=response.headers.get("content-type", "application/xml"),
            )  # fmt: skip
        load_filing(self, ctx, doc, tally)

    def check_shape(self, ctx: AdapterContext) -> None:
        """Canary: one live listing for the first universe symbol, validated strictly. Raises on drift."""
        symbols = self._universe(ctx)
        periods = ctx.settings.nse_results_listing_periods
        if not symbols or not periods:
            return
        with self._client(ctx) as http:
            warm_up_nse_session(http)
            response = http.get(
                ctx.settings.nse_results_listing_api_url, params=self._listing_params(symbols[0], periods[0])
            )
        parse_listing(response.content)

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        """Re-parse every stored XBRL file under the current RULE_VERSION from blob storage, never re-fetching."""
        tally = Tally()
        for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=ctx.now()):
            if "json" in raw.media_type:
                continue  # a stored listing, not a filing
            doc = RawDocument(
                data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
                as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
            )  # fmt: skip
            load_filing(self, ctx, doc, tally)
        return tally.result(self.name)
