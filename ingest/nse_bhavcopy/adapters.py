"""NSE bhavcopy adapters (CLAUDE.md "Data sources": price cross-check, dated ticker -> ISIN map).

Two sources of the same daily file, sharing one parser and one loader:
- NseBhavcopy: NSE's own archive host, official_archive, walking day by day
  from the UDiFF format's start to today. Unlike the indices lists, this
  archive already covers its whole history -- no Wayback source is needed.
- NseBhavcopyDrop: files downloaded by hand (manual_drop).

Every stored row becomes an `nse_bhavcopy_row`. Reads (core.db.pit) pick the
newest row per trade_date, so a reparse never rewrites history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx

from core.db.models import NseBhavcopyQuarantine, NseBhavcopyRow
from core.db.pit import (
    bhavcopy_latest_trade_date_as_of,
    bhavcopy_source_loaded_as_of,
    bhavcopy_sources_settled_as_of,
    raw_source_files_as_of,
)
from core.sources import SourceClass
from core.timezones import IST
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.http import AccessBlocked, PoliteClient
from ingest.nse_bhavcopy.parser import RULE_VERSION, UDIFF_START_DATE, BhavcopyRejected, parse_bhavcopy

TARGET_STORES = ("nse_bhavcopy_row", "nse_bhavcopy_quarantine")


# --------------------------------------------------------------------------- #
# Loader shared by every adapter
# --------------------------------------------------------------------------- #


@dataclass
class Tally:
    raw_files: int = 0
    rows_written: int = 0
    quarantined: int = 0
    rejected_files: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if any file was rejected or the run stopped early: both need a person."""
        detail = f"{self.rows_written} rows, {self.rejected_files} files rejected, {self.skipped} already loaded"
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        failed = bool(self.rejected_files or self.problems)
        return RunResult(
            adapter,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.rows_written,
            quarantined=self.quarantined,
        )


def _provenance(adapter: Adapter, doc: RawDocument) -> dict:
    return dict(
        as_of=doc.as_of,
        content_hash=doc.content_hash,
        source_url=doc.source_url,
        extracted_by=adapter.extracted_by(),
        model_version=None,
    )


def load_bhavcopy(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    """Parse one stored bhavcopy file into rows and quarantine entries.

    Skips a file already loaded under RULE_VERSION (correctness backstop; the
    archive adapter's own `settled` set additionally avoids re-fetching it).
    """
    tally.raw_files += 1
    if bhavcopy_source_loaded_as_of(
        ctx.session, source_url=doc.source_url, extracted_by=adapter.extracted_by(),
        rule_version=RULE_VERSION, as_of=doc.as_of,
    ):  # fmt: skip
        tally.skipped += 1
        return
    prov = _provenance(adapter, doc)
    try:
        parsed = parse_bhavcopy(doc.data)
    except BhavcopyRejected as exc:
        ctx.session.add(NseBhavcopyQuarantine(reason=exc.reason, detail=exc.detail, rule_version=RULE_VERSION, **prov))
        tally.rejected_files += 1
        tally.quarantined += 1
        return

    ctx.session.add_all(
        NseBhavcopyRow(
            trade_date=r.trade_date, isin=r.isin, symbol=r.symbol, series=r.series,
            open=r.open, high=r.high, low=r.low, close=r.close, prev_close=r.prev_close,
            volume=r.volume, turnover=r.turnover, trades=r.trades, rule_version=RULE_VERSION, **prov,
        )  # fmt: skip
        for r in parsed.rows
    )
    ctx.session.add_all(
        NseBhavcopyQuarantine(
            trade_date=parsed.trade_date, row_number=i.row_number, raw_row=i.raw_row,
            isin=i.isin, symbol=i.symbol, series=i.series, reason=i.reason, detail=i.detail,
            rule_version=RULE_VERSION, **prov,
        )  # fmt: skip
        for i in parsed.issues
    )
    tally.rows_written += len(parsed.rows)
    tally.quarantined += len(parsed.issues)


def reparse_stored_bhavcopy(adapter: Adapter, ctx: AdapterContext) -> RunResult:
    """Re-parse `adapter`'s already-stored raw files under the current rule version.

    Reads bytes back from blob storage; never re-fetches over the network
    (CLAUDE.md Stack). A file already parsed under RULE_VERSION would just be
    reprocessed harmlessly (the unique constraint on nse_bhavcopy_row is per
    rule_version, and a duplicate whole-file quarantine is a rare, cheap
    no-op) -- callers normally only reparse after RULE_VERSION changes.
    """
    tally = Tally()
    for raw in raw_source_files_as_of(ctx.session, extracted_by=adapter.extracted_by(), as_of=ctx.now()):
        doc = RawDocument(
            data=ctx.blob.get(raw.content_hash),
            content_hash=raw.content_hash,
            source_url=raw.source_url,
            as_of=raw.as_of,
            fetched_at=raw.fetched_at,
            media_type=raw.media_type,
        )
        load_bhavcopy(adapter, ctx, doc, tally)
    return tally.result(adapter.name)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()


def _day_end_ist(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=IST)


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class NseBhavcopy(Adapter):
    """NSE's own archive host: one zipped bhavcopy CSV per trade date.

    `as_of` is the date-only convention (temporal-model.md): 23:59:59 IST on
    the trade date, capped at fetch time so a same-day fetch never claims
    knowledge from later than it actually has it (R2). Walks every calendar
    day from the format's start to today; a 404 means "not a trading day"
    (weekend or holiday) and is skipped, not a block -- NSE's archive simply
    has no file for a day nothing traded, which is a structural fact about
    the archive, not anti-bot behaviour (CLAUDE.md "Breakage").
    """

    name = "nse_bhavcopy"
    source_class = SourceClass.OFFICIAL_ARCHIVE
    target_stores = TARGET_STORES

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return PoliteClient(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.nse_bhavcopy_min_interval_seconds,
            respect_robots=True,
            transport=self._transport,
        )

    def _url(self, ctx: AdapterContext, day: date) -> str:
        return ctx.settings.nse_bhavcopy_archive_url_template.format(date=day.strftime("%Y%m%d"))

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        settled = bhavcopy_sources_settled_as_of(
            ctx.session, extracted_by=self.extracted_by(), rule_version=RULE_VERSION, as_of=ctx.now()
        )
        latest_loaded = bhavcopy_latest_trade_date_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=ctx.now())
        today = ctx.now().date()
        with self._client(ctx) as http:
            current = latest_loaded + timedelta(days=1) if latest_loaded else UDIFF_START_DATE
            while current <= today:
                url = self._url(ctx, current)
                if url in settled:
                    tally.skipped += 1
                    current += timedelta(days=1)
                    continue
                try:
                    response = http.get(url)
                except AccessBlocked as exc:
                    tally.problems.append(f"stopped at {url}: {exc}")
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        current += timedelta(days=1)  # not a trading day
                        continue
                    tally.problems.append(f"stopped at {url}: {exc}")
                    break
                except httpx.HTTPError as exc:
                    tally.problems.append(f"stopped at {url}: {exc}")
                    break
                as_of = min(_day_end_ist(current), ctx.now())
                doc = self.store_raw(ctx, data=response.content, source_url=url, as_of=as_of,
                                      media_type=_media_type(response))  # fmt: skip
                load_bhavcopy(self, ctx, doc, tally)
                settled.add(url)
                current += timedelta(days=1)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        today = ctx.now().date()
        with self._client(ctx) as http:
            for offset in range(10):
                day = today - timedelta(days=offset)
                if day < UDIFF_START_DATE:
                    break
                try:
                    response = http.get(self._url(ctx, day))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    raise
                parse_bhavcopy(response.content)
                return
        raise RuntimeError(f"no bhavcopy file found in the 10 days up to {today}")

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_bhavcopy(self, ctx)


class NseBhavcopyDrop(DropFolderAdapter):
    """Bhavcopy files downloaded by hand into <EQUITY_DROP_FOLDER>/nse_bhavcopy_drop/.

    For a date the archive host cannot serve, or as a fallback if it ever
    blocks automated access. `published_at` in the sidecar is the trade
    date's day-end IST (temporal-model.md's date-only convention).
    """

    name = "nse_bhavcopy_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_bhavcopy(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _meta in self.pending(ctx):
            parse_bhavcopy(path.read_bytes())

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_bhavcopy(self, ctx)
