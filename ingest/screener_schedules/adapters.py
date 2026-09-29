"""Screener company schedules adapter (web_scrape for gross block source).

Fetches fixed asset schedules from Screener.in API to extract gross block values
not available in XBRL results.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from core.db.models import (
    Consolidation,
    FactKind,
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFactsQuarantineReason,
)
from core.db.pit import (
    index_universe_symbols_as_of,
    screener_schedules_fact_as_of,
    symbol_to_isin_as_of,
)
from core.sources import SourceClass
from ingest.base import Adapter, AdapterContext, RunResult, RunStatus
from ingest.http import AccessBlocked, PoliteClient
from ingest.screener_schedules.parser import RULE_VERSION, SchedulesParsingError, parse_schedules

TARGET_STORES = ("financial_facts",)

# Politeness: never faster than one request per 3 s, whatever the setting says.
MIN_INTERVAL_FLOOR_SECONDS = 3.0
_COMPANY_ID = re.compile(r'data-company-id="(\d+)"')
_SCHEDULES_PARAMS = {"parent": "Fixed Assets", "section": "balance-sheet", "consolidated": ""}


@dataclass
class Tally:
    """Track ingest progress."""

    companies_processed: int = 0
    facts_written: int = 0
    isin_unresolved: int = 0
    http_errors: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        detail = f"{self.companies_processed} companies, {self.facts_written} facts"
        if self.isin_unresolved:
            detail += f", {self.isin_unresolved} ISIN unresolved"
        if self.http_errors:
            detail += f", {self.http_errors} HTTP errors"
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        failed = bool(self.http_errors or self.problems)
        return RunResult(
            adapter,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=0,
            rows_written=self.facts_written,
        )


class ScreenerSchedules(Adapter):
    """Screener schedules web_scrape adapter.

    Fetches the fixed-assets schedules for the constituent-list universe to
    extract gross PPE. Off by default; refused at startup in commercial mode
    (web_scrape source class). No login, no retries on a block.
    """

    name = "screener_schedules"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return PoliteClient(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=max(
                ctx.settings.screener_schedules_min_interval_seconds, MIN_INTERVAL_FLOOR_SECONDS
            ),
            respect_robots=True,
            transport=self._transport,
            monotonic=self._monotonic,
            sleep=self._sleep,
        )

    def ingest(self, ctx: AdapterContext) -> RunResult:
        """For each universe symbol: company page -> ISIN -> schedules -> facts.

        The ISIN comes from the dated bhavcopy map; an unresolved symbol is
        quarantined, never guessed. A fact already stored with the same value
        is not written again, so a rerun writes nothing.
        """
        tally = Tally()
        now = ctx.now()
        base = ctx.settings.screener_schedules_base_url
        symbols = index_universe_symbols_as_of(ctx.session, as_of=now)

        with self._client(ctx) as http:
            for symbol in symbols:
                try:
                    self._ingest_symbol(ctx, http, tally, symbol, base, now)
                except AccessBlocked as e:
                    # A block is final: stop the run, never retry or work around it.
                    tally.problems.append(f"access blocked at {symbol}: {e}")
                    break

        ctx.session.flush()
        return tally.result(self.name)

    def _ingest_symbol(
        self,
        ctx: AdapterContext,
        http: PoliteClient,
        tally: Tally,
        symbol: str,
        base: str,
        now: datetime,
    ) -> None:
        company_url = f"{base}/company/{symbol}/consolidated/"
        try:
            response = http.get(company_url)
        except httpx.HTTPError as e:
            tally.http_errors += 1
            tally.problems.append(f"Failed to fetch {symbol} page: {e}")
            return
        try:
            page = self.store_raw(
                ctx, data=response.content, source_url=company_url, as_of=now, media_type="text/html"
            )
        except ValueError as e:
            tally.problems.append(f"Invalid as_of time for {symbol} page: {e}")
            return
        match = _COMPANY_ID.search(response.text)
        if not match:
            tally.problems.append(f"Could not extract data-company-id for {symbol}")
            return
        company_id = match.group(1)

        isin = symbol_to_isin_as_of(ctx.session, symbol=symbol, on=now.date(), as_of=now)
        if not isin:
            tally.isin_unresolved += 1
            ctx.session.add(
                FinancialFactsQuarantine(
                    isin=None,
                    reason=FinancialFactsQuarantineReason.ISIN_UNRESOLVED,
                    detail=f"Symbol {symbol} not in the dated symbol->ISIN map at {now}",
                    rule_version=RULE_VERSION,
                    as_of=now,
                    content_hash=page.content_hash,
                    source_url=company_url,
                    extracted_by=self.extracted_by(),
                    model_version=None,
                )
            )
            return

        schedules_url = f"{base}/api/company/{company_id}/schedules/"
        try:
            response = http.get(schedules_url, params=_SCHEDULES_PARAMS)
        except httpx.HTTPError as e:
            tally.http_errors += 1
            tally.problems.append(f"Failed to fetch {symbol} schedules: {e}")
            return
        try:
            raw_doc = self.store_raw(
                ctx,
                data=response.content,
                source_url=schedules_url,
                as_of=now,
                media_type="application/json",
            )
        except ValueError as e:
            tally.problems.append(f"Invalid as_of time for {symbol} schedules: {e}")
            return
        try:
            facts = parse_schedules(
                raw_doc.data, symbol=symbol, isin=isin, consolidation="consolidated"
            )
        except SchedulesParsingError as e:
            tally.problems.append(f"Failed to parse {symbol} schedules: {e}")
            return

        for fact in facts:
            existing = screener_schedules_fact_as_of(
                ctx.session,
                isin=isin,
                period_end=fact.period_end,
                line_item=fact.line_item,
                as_of=now,
            )
            if existing is not None and existing.value == fact.value:
                continue  # rerun: already stored
            ctx.session.add(
                FinancialFact(
                    isin=isin,
                    consolidation=Consolidation.CONSOLIDATED,
                    fact_kind=FactKind.COMPUTED,
                    line_item=fact.line_item,
                    xbrl_element=None,
                    period_start=None,
                    period_end=fact.period_end,
                    value=fact.value,
                    unit=fact.unit,
                    rule_version=RULE_VERSION,
                    as_of=raw_doc.as_of,
                    content_hash=raw_doc.content_hash,
                    source_url=raw_doc.source_url,
                    extracted_by=self.extracted_by(),
                    model_version=None,
                )
            )
            tally.facts_written += 1
        tally.companies_processed += 1

    def check_shape(self, ctx: AdapterContext) -> None:
        """Canary: fetch the TCS page and its schedules to validate the API shape."""
        base = ctx.settings.screener_schedules_base_url
        with self._client(ctx) as http:
            try:
                page = http.get(f"{base}/company/TCS/consolidated/")
            except httpx.HTTPError as e:
                raise RuntimeError(f"Failed to fetch TCS page: {e}") from e
            match = _COMPANY_ID.search(page.text)
            if not match:
                raise RuntimeError("data-company-id not found in TCS page")
            try:
                response = http.get(
                    f"{base}/api/company/{match.group(1)}/schedules/", params=_SCHEDULES_PARAMS
                )
                data = response.json()
            except (httpx.HTTPError, ValueError) as e:
                raise RuntimeError(f"Failed to fetch TCS schedules: {e}") from e
            if not isinstance(data, dict):
                raise RuntimeError(f"Expected dict, got {type(data).__name__}")
