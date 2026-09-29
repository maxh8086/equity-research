"""Automated Screener workbook export (Session 6): a web_scrape source, off by default.

The validation gate compares our computed numbers with Screener's own export.
`screener_export_drop` reads workbooks saved by hand; this adapter fetches the
same workbooks itself, for each company of the validation sample
(`validate/samples.py`), then hands them to the drop adapter's loader
(`load_export`), so `screener_export` and `screener_value` rows are produced
exactly as for a hand drop. The parser, identity checks and R2 checks are not
touched here.

Flow, one identified session, at least 3 s between requests:
  1. GET /login/ for the CSRF token, POST the login (credentials from
     SCREENER_USERNAME / SCREENER_PASSWORD; never logged or echoed);
  2. per company: GET its page (carries `data-company-id` and a CSRF token),
     POST the export form with that token, read the workbook bytes;
  3. store the bytes raw (`as_of` = the download time: Screener builds the file
     on request, as for a hand drop), then `load_export`.

The ISIN is never guessed. The sample names the ISIN and the constituent lists
must give exactly that ISIN for the symbol, else the company is quarantined:
no request is made for it, nothing is written, and the run reports it.

Stops the whole run, without retry, on a block (401/403/429/451, robots
disallow), a failed login, or any redirect back to /login/. No proxies, no
fingerprinting, no CAPTCHA handling. A rerun that downloads the same bytes for
the same URL stores nothing new and loads nothing new.

NOT VERIFIED LIVE. The endpoint path and form fields below are Screener's
documented behaviour as far as it could be established without a request (login
page CSRF flow: as used by the user's Kite-ATH-Scanner client; company id
attribute: as used by `screener_schedules`; the export route and form: from
memory of the public site, not confirmed). They are isolated as constants so
the first live run corrects them in one place. Use requires Screener's terms to
permit automated, logged-in export; the person enabling it confirms that.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

import httpx

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import Consolidation
from core.db.pit import (
    index_symbol_isins_as_of,
    raw_source_file_by_hash_url_as_of,
    raw_source_files_as_of,
    screener_exports_as_of,
)
from core.sources import SourceClass
from ingest.base import Adapter, AdapterContext, RawDocument, RunResult, RunStatus
from ingest.http import AccessBlocked
from ingest.screener_export.adapters import INDEX_LIST_WINDOW, TARGET_STORES, XLSX, Tally, load_export
from ingest.screener_export_live.session import LoginRedirect, ScreenerSession

# --- Screener's site shape: the parts that are unverified live ------------------------------- #
LOGIN_PATH = "/login/"
LOGIN_USERNAME_FIELD = "username"
LOGIN_PASSWORD_FIELD = "password"
LOGIN_NEXT_FIELD, LOGIN_NEXT_VALUE = "next", "/"
CSRF_FIELD = "csrfmiddlewaretoken"  # hidden input on the login page and on the company page's forms
CSRF_COOKIE = "csrftoken"  # Django's cookie, the fallback when a page carries no hidden input
COMPANY_PAGE_PATH = "/company/{symbol}/{view}"  # view: "consolidated/" or ""
COMPANY_ID = re.compile(r'data-company-id="(\d+)"')
EXPORT_PATH = "/user/company/export/{company_id}/"  # POST, CSRF-protected, answers the workbook
# ---------------------------------------------------------------------------------------------- #

MIN_INTERVAL_FLOOR_SECONDS = 3.0
XLSX_MAGIC = b"PK\x03\x04"  # an .xlsx is a zip; anything else (an HTML error page) is not a workbook
_CSRF_INPUT = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_NAME = re.compile(rf'name=["\']{CSRF_FIELD}["\']', re.IGNORECASE)
_VALUE = re.compile(r'value=["\']([^"\']+)["\']', re.IGNORECASE)


class LoginFailed(Exception):
    """The login was refused. The message never carries the credentials."""


def csrf_token(html: str, http: ScreenerSession) -> str | None:
    """The hidden CSRF input on a page, else Django's csrftoken cookie, else None."""
    for tag in _CSRF_INPUT.findall(html):
        if _NAME.search(tag) and (value := _VALUE.search(tag)):
            return value.group(1)
    return http.cookie(CSRF_COOKIE)


def default_companies() -> list[tuple[str, str]]:
    """(isin, symbol) for every company of the current validation sample, in draw order."""
    from validate.samples import CURRENT

    return [(isin, CURRENT.symbols[isin]) for isin in CURRENT.isins()]


class ScreenerExportLive(Adapter):
    """Screener exports fetched with a logged-in session. Off unless the switch and web scraping are on."""

    name = "screener_export_live"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        companies: Sequence[tuple[str, str]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """`transport`, `monotonic` and `sleep` are for tests; `companies` overrides the sample, for a targeted run."""
        self._transport = transport
        self._companies = list(companies) if companies is not None else None
        self._monotonic = monotonic
        self._sleep = sleep

    def companies(self) -> list[tuple[str, str]]:
        return self._companies if self._companies is not None else default_companies()

    @staticmethod
    def _credentials(settings: Settings) -> tuple[str, str] | None:
        user, password = settings.screener_username, settings.screener_password
        if user is None or password is None:
            return None
        pair = (user.get_secret_value(), password.get_secret_value())
        return pair if all(pair) else None

    def _session(self, ctx: AdapterContext) -> ScreenerSession:
        s = ctx.settings
        return ScreenerSession(
            user_agent=s.http_user_agent,
            min_interval_seconds=max(s.screener_export_live_min_interval_seconds, MIN_INTERVAL_FLOOR_SECONDS),
            backoff_seconds=s.screener_export_live_backoff_seconds,
            transport=self._transport,
            monotonic=self._monotonic,
            sleep=self._sleep,
        )

    def ingest(self, ctx: AdapterContext) -> RunResult:
        credentials = self._credentials(ctx.settings)
        if credentials is None:
            return RunResult(
                self.name,
                RunStatus.FAILED,
                "refusing to run: SCREENER_USERNAME and SCREENER_PASSWORD must both be set (in the local .env)",
            )
        tally = Tally()
        now = ctx.now()
        consolidation = (
            Consolidation.CONSOLIDATED if ctx.settings.screener_export_live_consolidated else Consolidation.STANDALONE
        )
        wanted, quarantined = [], 0
        for isin, symbol in self.companies():
            listed = index_symbol_isins_as_of(
                ctx.session, symbol=symbol, since=now - INDEX_LIST_WINDOW, as_of=now
            )
            if listed == {isin}:
                wanted.append((isin, symbol))
            else:
                quarantined += 1
                tally.problems.append(
                    f"{symbol}: constituent lists give {sorted(listed)}, the sample says {isin}; "
                    "quarantined, nothing requested"
                )
        if wanted:
            self._run_session(ctx, credentials, wanted, consolidation, tally)
        result = tally.result(self.name)
        return dataclasses.replace(
            result, quarantined=quarantined, detail=f"{len(wanted)} of {len(wanted) + quarantined} companies requested; "
            + result.detail
        )

    def _run_session(
        self,
        ctx: AdapterContext,
        credentials: tuple[str, str],
        wanted: list[tuple[str, str]],
        consolidation: Consolidation,
        tally: Tally,
    ) -> None:
        base = ctx.settings.screener_export_live_base_url.rstrip("/")
        with self._session(ctx) as http:
            try:
                self._login(http, base, *credentials)
                for isin, symbol in wanted:
                    self._export_one(ctx, http, base, isin, symbol, consolidation, tally)
            except AccessBlocked as exc:
                tally.problems.append(f"Screener blocked the run, stopping: {exc}")
            except LoginRedirect as exc:
                tally.problems.append(f"session lost, stopping (no re-login): {exc}")
            except LoginFailed as exc:
                tally.problems.append(str(exc))
            except httpx.HTTPError as exc:
                tally.problems.append(f"login or session failed: {type(exc).__name__}: {exc}")

    def _login(self, http: ScreenerSession, base: str, username: str, password: str) -> None:
        login_url = base + LOGIN_PATH
        page = http.get(login_url, allow_login_page=True)
        token = csrf_token(page.text, http)
        if token is None:
            raise LoginFailed("Screener login failed: the login page carried no CSRF token")
        response = http.post(
            login_url,
            data={CSRF_FIELD: token, LOGIN_USERNAME_FIELD: username, LOGIN_PASSWORD_FIELD: password,
                  LOGIN_NEXT_FIELD: LOGIN_NEXT_VALUE},  # fmt: skip
            referer=login_url,
            follow=False,
            allow_login_page=True,
        )
        # Success redirects away from the login page; a refusal re-renders it or redirects back to it.
        location = response.headers.get("location", "")
        if not (300 <= response.status_code < 400) or urlsplit(location).path.startswith(LOGIN_PATH):
            raise LoginFailed("Screener login failed: check SCREENER_USERNAME and SCREENER_PASSWORD")

    def _export_one(
        self,
        ctx: AdapterContext,
        http: ScreenerSession,
        base: str,
        isin: str,
        symbol: str,
        consolidation: Consolidation,
        tally: Tally,
    ) -> None:
        """One company. A block, a lost session or a login redirect propagates and stops the run."""
        view = "consolidated/" if consolidation is Consolidation.CONSOLIDATED else ""
        page_url = base + COMPANY_PAGE_PATH.format(symbol=symbol, view=view)
        try:
            page = http.get(page_url)
            match = COMPANY_ID.search(page.text)
            if match is None:
                tally.problems.append(f"{symbol}: no company id on {page_url}; skipped")
                return
            token = csrf_token(page.text, http)
            if token is None:
                tally.problems.append(f"{symbol}: no CSRF token on {page_url}; skipped")
                return
            export_url = base + EXPORT_PATH.format(company_id=match.group(1))
            response = http.post(export_url, data={CSRF_FIELD: token}, referer=page_url)
        except httpx.HTTPError as exc:
            tally.problems.append(f"{symbol}: {type(exc).__name__}: {exc}")
            return
        if not response.content.startswith(XLSX_MAGIC):
            tally.problems.append(f"{symbol}: the export answer is not an .xlsx workbook; not stored")
            return
        doc = self._store(ctx, response.content, page_url)
        load_export(self, ctx, doc, isin, consolidation, tally)

    def _store(self, ctx: AdapterContext, data: bytes, page_url: str) -> RawDocument:
        """Store the workbook raw, unless these exact bytes are already stored for this URL."""
        stored = raw_source_file_by_hash_url_as_of(
            ctx.session, content_hash=content_hash(data), source_url=page_url,
            extracted_by=self.extracted_by(), as_of=ctx.now(),
        )  # fmt: skip
        if stored is None:
            return self.store_raw(ctx, data=data, source_url=page_url, as_of=ctx.now(), media_type=XLSX)
        return RawDocument(
            data=ctx.blob.get(stored.content_hash), content_hash=stored.content_hash, source_url=stored.source_url,
            as_of=stored.as_of, fetched_at=stored.fetched_at, media_type=stored.media_type,
        )  # fmt: skip

    def check_shape(self, ctx: AdapterContext) -> None:
        """Canary without a request: a logged-in fetch every day is not polite. Fails if the run could not start."""
        if self._credentials(ctx.settings) is None:
            raise RuntimeError("SCREENER_USERNAME and SCREENER_PASSWORD must both be set")
        if not self.companies():
            raise RuntimeError("no companies to export")

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        """Re-parse every stored workbook under the current RULE_VERSION from blob storage, never re-fetching."""
        tally = Tally()
        now = ctx.now()
        known = {
            (e.content_hash, e.as_of): (e.isin, e.consolidation) for e in screener_exports_as_of(ctx.session, as_of=now)
        }
        for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=now):
            company = known.get((raw.content_hash, raw.as_of))
            if company is None:
                tally.problems.append(f"{raw.source_url} ({raw.content_hash[:12]}): never loaded; rerun the export")
                continue
            doc = RawDocument(
                data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
                as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
            )  # fmt: skip
            load_export(self, ctx, doc, *company, tally)
        return tally.result(self.name)
