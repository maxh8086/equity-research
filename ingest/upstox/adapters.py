"""Upstox API v3 daily candle adapters (CLAUDE.md "Data sources": daily prices).

Two sources of the same response shape, sharing one parser and one loader:
- UpstoxDailyCandles: the API itself, official_api, one ISIN at a time over
  the whole index universe (every ISIN in any Nifty 50 / Next 50 list known).
- UpstoxDailyCandlesDrop: responses saved by hand (manual_drop), e.g. while
  no API app exists. The sidecar's `source_url` is the request URL and its
  `published_at` the time the response was downloaded.

`as_of` is the fetch time, not the trade date: a vendor history can be
revised after the fact (adjusted for a later split or bonus), so a response
is only known to be the vendor's view from the moment it arrived (R2). Whether
the candles equal the exchange's as-traded prices is checked against NSE
bhavcopy by core.db.pit.upstox_bhavcopy_crosscheck_as_of, never assumed.

Broker MCP tools are never used here (CLAUDE.md "Integrations"): this is
plain HTTP with a token a human obtained by logging in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from core.db.models import UpstoxCandle, UpstoxCandleQuarantine
from core.db.pit import (
    index_universe_isins_as_of,
    raw_source_files_as_of,
    upstox_latest_trade_date_as_of,
    upstox_source_loaded_as_of,
)
from core.sources import SourceClass
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.http import AccessBlocked, PoliteClient
from ingest.upstox.parser import (
    RULE_VERSION,
    CandlesRejected,
    candles_url,
    instrument_key,
    last_complete_day,
    parse_candles,
    request_windows,
    require_isin,
)

TARGET_STORES = ("upstox_candle", "upstox_candle_quarantine")

# The canary's instrument: Reliance Industries, listed throughout the history.
CANARY_ISIN = "INE002A01018"
CANARY_DAYS = 14


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
        """Failed if any response was rejected or a request failed: both need a person."""
        detail = (
            f"{self.rows_written} candles, {self.rejected_files} responses rejected, "
            f"{self.skipped} already loaded"
        )
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


def load_candles(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    """Parse one stored response into candles and quarantine entries.

    A response is identified by (source_url, as_of); one already loaded under
    RULE_VERSION is skipped, so a drop-folder rerun or a reparse is a no-op.
    """
    tally.raw_files += 1
    if upstox_source_loaded_as_of(
        ctx.session, source_url=doc.source_url, fetched_as_of=doc.as_of,
        extracted_by=adapter.extracted_by(), rule_version=RULE_VERSION,
    ):  # fmt: skip
        tally.skipped += 1
        return
    prov = _provenance(adapter, doc)
    try:
        isin = require_isin(doc.source_url)
        parsed = parse_candles(doc.data, last_complete=last_complete_day(doc.as_of))
    except CandlesRejected as exc:
        ctx.session.add(
            UpstoxCandleQuarantine(isin=None, reason=exc.reason, detail=exc.detail, rule_version=RULE_VERSION, **prov)
        )
        tally.rejected_files += 1
        tally.quarantined += 1
        return

    ctx.session.add_all(
        UpstoxCandle(
            isin=isin, instrument_key=instrument_key(isin), trade_date=c.trade_date,
            open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
            open_interest=c.open_interest, rule_version=RULE_VERSION, **prov,
        )  # fmt: skip
        for c in parsed.candles
    )
    ctx.session.add_all(
        UpstoxCandleQuarantine(
            isin=isin, trade_date=i.trade_date, candle_index=i.candle_index, raw_candle=i.raw_candle,
            reason=i.reason, detail=i.detail, rule_version=RULE_VERSION, **prov,
        )  # fmt: skip
        for i in parsed.issues
    )
    ctx.session.flush()
    tally.rows_written += len(parsed.candles)
    tally.quarantined += len(parsed.issues)


def reparse_stored_candles(adapter: Adapter, ctx: AdapterContext) -> RunResult:
    """Re-parse `adapter`'s stored responses under the current rule version.

    Reads bytes back from blob storage; never re-fetches (CLAUDE.md Stack).
    Each response keeps its own fetch time as `as_of`.
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
        load_candles(adapter, ctx, doc, tally)
    return tally.result(adapter.name)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "application/json").split(";")[0].strip()


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class MissingToken(Exception):
    pass


class UpstoxDailyCandles(Adapter):
    """Upstox API v3 historical daily candles for every ISIN in the index universe.

    Per ISIN, requests walk fixed decade windows from 2000-01-01 (the API's
    per-request limit for daily candles). A past window's URL never changes,
    so once fetched it is skipped; the current window starts the day after the
    latest stored candle. A 401/403/429 stops the run (the daily token has
    expired, or the API is refusing us); nothing is retried around it.
    """

    name = "upstox_daily_candles"
    source_class = SourceClass.OFFICIAL_API
    target_stores = TARGET_STORES

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return PoliteClient(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.upstox_min_interval_seconds,
            respect_robots=False,  # an official API, governed by its terms
            timeout_seconds=ctx.settings.upstox_timeout_seconds,
            transport=self._transport,
        )

    def _headers(self, ctx: AdapterContext) -> dict[str, str]:
        token = ctx.settings.upstox_access_token
        if token is None or not token.get_secret_value():
            raise MissingToken("EQUITY_UPSTOX_ACCESS_TOKEN is not set (log in to Upstox; tokens expire daily)")
        return {"Authorization": f"Bearer {token.get_secret_value()}", "Accept": "application/json"}

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        try:
            headers = self._headers(ctx)
        except MissingToken as exc:
            tally.problems.append(str(exc))
            return tally.result(self.name)
        now = ctx.now()
        isins = sorted(index_universe_isins_as_of(ctx.session, as_of=now))
        if not isins:
            tally.problems.append("no ISINs: load index constituent lists first")
            return tally.result(self.name)
        fetched = {
            raw.source_url for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=now)
        }
        end = last_complete_day(now)
        with self._client(ctx) as http:
            for isin in isins:
                cursor = upstox_latest_trade_date_as_of(
                    ctx.session, isin=isin, extracted_by=self.extracted_by(), as_of=now
                )
                for start, stop in request_windows(end, cursor):
                    url = candles_url(ctx.settings.upstox_base_url, isin, start, stop)
                    if url in fetched:
                        tally.skipped += 1
                        continue
                    try:
                        response = http.get(url, headers=headers)
                    except AccessBlocked as exc:
                        tally.problems.append(f"stopped at {isin}: {exc}")
                        return tally.result(self.name)
                    except httpx.HTTPStatusError as exc:
                        if 400 <= exc.response.status_code < 500:
                            # e.g. an ISIN Upstox does not list (an old pre-split ISIN):
                            # that instrument needs a person; the rest can proceed.
                            tally.problems.append(f"{isin} {start}..{stop}: HTTP {exc.response.status_code}")
                            break
                        tally.problems.append(f"stopped at {isin}: {exc}")
                        return tally.result(self.name)
                    except httpx.HTTPError as exc:
                        tally.problems.append(f"stopped at {isin}: {exc}")
                        return tally.result(self.name)
                    doc = self.store_raw(ctx, data=response.content, source_url=url, as_of=now,
                                         media_type=_media_type(response))  # fmt: skip
                    load_candles(self, ctx, doc, tally)
                    fetched.add(url)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        headers = self._headers(ctx)
        end = last_complete_day(ctx.now())
        url = candles_url(ctx.settings.upstox_base_url, CANARY_ISIN, end - timedelta(days=CANARY_DAYS), end)
        with self._client(ctx) as http:
            response = http.get(url, headers=headers)
        parsed = parse_candles(response.content, last_complete=end)
        if not parsed.candles:
            raise RuntimeError(f"no candles for {CANARY_ISIN} in the {CANARY_DAYS} days to {end}")
        if parsed.issues:
            raise RuntimeError(f"{len(parsed.issues)} candles failed validation: {parsed.issues[0].detail}")

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_candles(self, ctx)


class UpstoxDailyCandlesDrop(DropFolderAdapter):
    """Candle responses saved by hand into <EQUITY_DROP_FOLDER>/upstox_daily_candles_drop/.

    Each file is one API response body; its sidecar's `source_url` must be
    the request URL (it names the ISIN) and `published_at` the download time,
    by the same fetch-time convention as the API adapter.
    """

    name = "upstox_daily_candles_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_candles(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, meta in self.pending(ctx):
            require_isin(meta.source_url)
            parse_candles(path.read_bytes(), last_complete=last_complete_day(meta.published_at))

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_candles(self, ctx)
