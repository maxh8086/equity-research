"""Live Screener export adapter, against a mock transport: no test touches the network.

The mock plays the parts of Screener the adapter depends on: a login page with a
CSRF token, a session cookie set by a successful login, company pages carrying
`data-company-id` and a form CSRF token, and a CSRF-protected export POST that
answers with a workbook. Only the fake values 'test-user' / 'test-pass' exist here.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import func, select

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    Consolidation,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    RawSourceFile,
    ScreenerExport,
    ScreenerValue,
)
from core.db.pit import screener_exports_as_of
from core.sources import DeploymentMode, SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.registry import StartupRefused, check_startup, discover
from ingest.screener_export_live import adapters as live
from ingest.screener_export_live.adapters import ScreenerExportLive
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

FIXTURE = Path(__file__).parent / "fixtures" / "screener_export" / "vinati_data_sheet_v2_1.xlsx"
NAME = "screener_export_live"
USER, PASSWORD = "test-user", "test-pass"
LOGIN_TOKEN = "tok-login"
NOW = datetime(2022, 11, 20, 10, 0, tzinfo=IST)
LISTED = datetime(2022, 9, 30, 23, 59, 59, tzinfo=IST)
VINATI, IOC, GAIL = "INE410B01037", "INE242A01010", "INE129A01019"
COMPANIES = [(VINATI, "VINATIORGA"), (IOC, "IOC"), (GAIL, "GAIL")]
IDS = {"VINATIORGA": "1001", "IOC": "1002", "GAIL": "1003"}
XLSX_BYTES = FIXTURE.read_bytes()


# --------------------------------------------------------------------------- #
# The fake Screener
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=f"<html><body>{body}</body></html>", headers={"content-type": "text/html"})


class FakeScreener:
    """Records every request; `override` may answer first (to inject a block, a redirect, a 503)."""

    def __init__(self, clock: FakeClock, override: Callable[[httpx.Request], httpx.Response | None] | None = None,
                 export_body: bytes = XLSX_BYTES, login_ok: bool = True) -> None:  # fmt: skip
        self.clock = clock
        self.override = override
        self.export_body = export_body
        self.login_ok = login_ok
        self.log: list[dict] = []

    def paths(self) -> list[str]:
        return [f"{r['method']} {r['path']}" for r in self.log]

    def times(self) -> list[float]:
        return [r["t"] for r in self.log]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        self.log.append({"t": self.clock.monotonic(), "method": request.method, "path": request.url.path,
                         "form": form, "headers": dict(request.headers)})  # fmt: skip
        if self.override is not None and (answer := self.override(request)) is not None:
            return answer
        path, cookie = request.url.path, request.headers.get("cookie", "")
        logged_in = "sessionid=" in cookie
        if path == "/robots.txt":
            return httpx.Response(404)
        if path == "/login/" and request.method == "GET":
            return httpx.Response(
                200,
                text=f'<form><input type="hidden" name="csrfmiddlewaretoken" value="{LOGIN_TOKEN}"></form>',
                headers={"set-cookie": "csrftoken=cookie-token; Path=/"},
            )
        if path == "/login/":
            if self.login_ok and form.get("csrfmiddlewaretoken") == LOGIN_TOKEN \
                    and form.get("username") == USER and form.get("password") == PASSWORD:  # fmt: skip
                return httpx.Response(302, headers={"location": "/", "set-cookie": "sessionid=sess-1; Path=/"})
            return _html('<form action="/login/">wrong credentials</form>')
        if path == "/":
            return _html("home")
        if not logged_in:
            return httpx.Response(302, headers={"location": f"/login/?next={path}"})
        for symbol, cid in IDS.items():
            if path in (f"/company/{symbol}/consolidated/", f"/company/{symbol}/"):
                return _html(
                    f'<div id="company-info" data-company-id="{cid}"></div>'
                    f'<form method="post" action="{live.EXPORT_PATH.format(company_id=cid)}">'
                    f'<input type="hidden" name="csrfmiddlewaretoken" value="tok-page-{cid}"></form>'
                )
            if path == live.EXPORT_PATH.format(company_id=cid) and request.method == "POST":
                if form.get("csrfmiddlewaretoken") != f"tok-page-{cid}":
                    return httpx.Response(403, text="CSRF verification failed")
                return httpx.Response(200, content=self.export_body, headers={"content-type": live.XLSX})
        return httpx.Response(404)


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _settings(**kw) -> Settings:
    base = dict(web_scraping_enabled=True, source_switches={NAME: True}, screener_username=USER,
                screener_password=PASSWORD)  # fmt: skip
    return Settings(**{**base, **kw})


def _ctx(session, now=NOW, blob=None, settings=None) -> AdapterContext:
    return AdapterContext(session, blob or MemoryBlobStore(), settings or _settings(), now=lambda: now)


def _index_list(session, symbol: str, isin: str, published: datetime = LISTED) -> None:
    prov = dict(as_of=published, content_hash=content_hash(f"{symbol}{isin}{published}".encode()),
                source_url=f"list-{symbol}-{published}", extracted_by="tests", model_version=None)  # fmt: skip
    snapshot = IndexSnapshot(index_code=IndexCode.NIFTY_NEXT_50, constituent_count=1, quarantined_rows=0,
                             rule_version="t", **prov)  # fmt: skip
    session.add(snapshot)
    session.flush()
    session.add(
        IndexSnapshotConstituent(snapshot_id=snapshot.id, isin=isin, symbol=symbol, series="EQ", company_name="C",
                                 industry="I", row_number=1, **prov)  # fmt: skip
    )
    session.flush()


def _adapter(server: FakeScreener, companies=COMPANIES) -> ScreenerExportLive:
    return ScreenerExportLive(transport=httpx.MockTransport(server), companies=companies,
                              monotonic=server.clock.monotonic, sleep=server.clock.sleep)  # fmt: skip


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def listed(session):
    for isin, symbol in COMPANIES:
        _index_list(session, symbol, isin)


# --------------------------------------------------------------------------- #
# Declaration and switches
# --------------------------------------------------------------------------- #


def test_declared_as_web_scrape_adapter():
    assert ScreenerExportLive.name == NAME
    assert ScreenerExportLive.source_class is SourceClass.WEB_SCRAPE
    assert set(ScreenerExportLive.target_stores) == {"screener_export", "screener_value"}
    assert discover()[NAME] is ScreenerExportLive


def test_default_off_makes_no_request(session, clock):
    server = FakeScreener(clock)
    result = _adapter(server).run(_ctx(session, settings=_settings(source_switches={})))
    assert result.status is RunStatus.DISABLED and server.log == []


def test_off_without_web_scraping_flag(session, clock):
    server = FakeScreener(clock)
    result = _adapter(server).run(_ctx(session, settings=_settings(web_scraping_enabled=False)))
    assert result.status is RunStatus.DISABLED and server.log == []


def test_commercial_mode_refuses_the_switch():
    settings = _settings(deployment_mode=DeploymentMode.COMMERCIAL, web_scraping_enabled=False)
    with pytest.raises(StartupRefused, match=NAME):
        check_startup(settings, {NAME: ScreenerExportLive})


def test_credentials_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("SCREENER_USERNAME", "env-user")
    monkeypatch.setenv("SCREENER_PASSWORD", "env-pass")
    settings = Settings(_env_file=None)
    assert settings.screener_username.get_secret_value() == "env-user"
    assert settings.screener_password.get_secret_value() == "env-pass"
    assert "env-pass" not in repr(settings) and "env-user" not in repr(settings)


# --------------------------------------------------------------------------- #
# Missing credentials
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", [{"screener_username": None}, {"screener_password": None},
                                     {"screener_username": None, "screener_password": None}])  # fmt: skip
def test_missing_credentials_refuse_to_run(session, clock, listed, missing):
    server = FakeScreener(clock)
    result = _adapter(server).run(_ctx(session, settings=_settings(**missing)))
    assert result.status is RunStatus.FAILED
    assert "SCREENER_USERNAME" in result.detail and "SCREENER_PASSWORD" in result.detail
    assert USER not in result.detail and PASSWORD not in result.detail
    assert server.log == []  # not even the login page
    assert _count(session, RawSourceFile) == 0


def test_blank_credentials_count_as_missing(session, clock, listed):
    server = FakeScreener(clock)
    result = _adapter(server).run(_ctx(session, settings=_settings(screener_username="", screener_password="")))
    assert result.status is RunStatus.FAILED and server.log == []


# --------------------------------------------------------------------------- #
# Login and CSRF
# --------------------------------------------------------------------------- #


def test_login_then_export_downloads_workbook_into_the_drop_rows(session, clock, listed):
    server = FakeScreener(clock)
    result = _adapter(server, COMPANIES[:1]).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert result.raw_files == 1 and result.rows_written == 431

    # The rows are the ones a hand drop produces: same loader, same store.
    exports = screener_exports_as_of(session, as_of=NOW, isin=VINATI)
    assert len(exports) == 1
    export = exports[0]
    assert export.consolidation is Consolidation.CONSOLIDATED
    assert export.as_of == NOW and export.content_hash == content_hash(XLSX_BYTES)
    assert export.source_url == "https://www.screener.in/company/VINATIORGA/consolidated/"
    assert export.extracted_by == ScreenerExportLive.extracted_by()
    assert _count(session, ScreenerValue) == 431

    raw = session.scalars(select(RawSourceFile)).one()
    assert raw.content_hash == content_hash(XLSX_BYTES) and raw.media_type == live.XLSX
    assert raw.source_url == export.source_url and raw.as_of == NOW


def test_login_posts_the_page_csrf_token_and_referer(session, clock, listed):
    server = FakeScreener(clock)
    _adapter(server, COMPANIES[:1]).run(_ctx(session))
    post = next(r for r in server.log if r["method"] == "POST" and r["path"] == "/login/")
    assert post["form"]["csrfmiddlewaretoken"] == LOGIN_TOKEN
    assert post["form"]["username"] == USER and post["form"]["password"] == PASSWORD
    assert post["headers"]["referer"] == "https://www.screener.in/login/"
    assert server.paths()[:3] == ["GET /robots.txt", "GET /login/", "POST /login/"]


def test_export_posts_the_company_page_csrf_token_and_referer(session, clock, listed):
    server = FakeScreener(clock)
    _adapter(server, COMPANIES[:1]).run(_ctx(session))
    export_path = live.EXPORT_PATH.format(company_id="1001")
    post = next(r for r in server.log if r["method"] == "POST" and r["path"] == export_path)
    assert post["form"]["csrfmiddlewaretoken"] == "tok-page-1001"
    assert post["headers"]["referer"] == "https://www.screener.in/company/VINATIORGA/consolidated/"
    assert "sessionid=sess-1" in post["headers"]["cookie"]


def test_honest_user_agent_on_every_request(session, clock, listed):
    server = FakeScreener(clock)
    _adapter(server, COMPANIES[:1]).run(_ctx(session))
    agents = {r["headers"]["user-agent"] for r in server.log}
    assert agents == {Settings().http_user_agent}


def test_failed_login_stops_before_any_company_request(session, clock, listed):
    server = FakeScreener(clock, login_ok=False)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "login failed" in result.detail.lower()
    assert USER not in result.detail and PASSWORD not in result.detail
    assert not any("/company/" in p or "/user/" in p for p in server.paths())
    assert _count(session, RawSourceFile) == 0 and _count(session, ScreenerExport) == 0


# --------------------------------------------------------------------------- #
# Stop conditions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_block_on_a_company_page_stops_the_run(session, clock, listed, status):
    def block(request):
        if request.url.path == "/company/IOC/consolidated/":
            return httpx.Response(status)

    server = FakeScreener(clock, override=block)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED and str(status) in result.detail
    assert not any("GAIL" in p for p in server.paths())  # the third company is never asked for
    assert _count(session, ScreenerExport) == 1  # only the first, already loaded


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_block_on_the_export_post_stops_the_run(session, clock, listed, status):
    def block(request):
        if request.method == "POST" and request.url.path == live.EXPORT_PATH.format(company_id="1002"):
            return httpx.Response(status)

    server = FakeScreener(clock, override=block)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    assert not any("GAIL" in p for p in server.paths())
    assert server.paths().count("POST " + live.EXPORT_PATH.format(company_id="1002")) == 1  # no retry


@pytest.mark.parametrize("status", [401, 403, 429, 451])
def test_block_on_login_stops_everything(session, clock, listed, status):
    def block(request):
        if request.url.path == "/login/" and request.method == "POST":
            return httpx.Response(status)

    server = FakeScreener(clock, override=block)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    assert not any("/company/" in p for p in server.paths())


def test_robots_disallow_stops_the_run(session, clock, listed):
    def robots(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /user/\n")

    server = FakeScreener(clock, override=robots)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "robots" in result.detail
    assert not any(p.startswith("POST /user/") for p in server.paths())
    assert _count(session, ScreenerExport) == 0


def test_redirect_back_to_login_mid_run_stops_the_run(session, clock, listed):
    def expire(request):
        if request.url.path == "/company/IOC/consolidated/":
            return httpx.Response(302, headers={"location": "/login/?next=/company/IOC/consolidated/"})

    server = FakeScreener(clock, override=expire)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "login" in result.detail.lower()
    assert not any("GAIL" in p for p in server.paths())
    assert server.paths().count("POST /login/") == 1  # no silent re-login
    assert _count(session, ScreenerExport) == 1


def test_redirect_to_login_on_the_export_post_stops_the_run(session, clock, listed):
    def expire(request):
        if request.method == "POST" and request.url.path == live.EXPORT_PATH.format(company_id="1001"):
            return httpx.Response(302, headers={"location": "/login/"})

    server = FakeScreener(clock, override=expire)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    assert not any("IOC" in p for p in server.paths())
    assert _count(session, ScreenerExport) == 0


def test_overload_gets_one_polite_backoff_then_succeeds(session, clock, listed):
    calls = []

    def flaky(request):
        if request.method == "POST" and request.url.path == live.EXPORT_PATH.format(company_id="1001"):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(503, headers={"retry-after": "5"})

    server = FakeScreener(clock, override=flaky)
    result = _adapter(server, COMPANIES[:1]).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert len(calls) == 2
    assert max(clock.sleeps) >= _settings().screener_export_live_backoff_seconds


def test_persistent_overload_is_a_problem_not_a_block(session, clock, listed):
    def down(request):
        if request.url.path == "/company/IOC/consolidated/":
            return httpx.Response(503)

    server = FakeScreener(clock, override=down)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "IOC" in result.detail
    assert server.paths().count("GET /company/IOC/consolidated/") == 2  # one retry, no more
    assert any("GAIL" in p for p in server.paths())  # the run went on
    # The fake serves one workbook for every company, so GAIL's copy counts as already loaded (same bytes).
    assert "POST " + live.EXPORT_PATH.format(company_id="1003") in server.paths()
    assert _count(session, ScreenerExport) == 1


def test_non_workbook_answer_is_not_stored(session, clock, listed):
    server = FakeScreener(clock, export_body=b"<html>please try again</html>")
    blob = MemoryBlobStore()
    result = _adapter(server, COMPANIES[:1]).run(_ctx(session, blob=blob))
    assert result.status is RunStatus.FAILED and "VINATIORGA" in result.detail
    assert _count(session, RawSourceFile) == 0 and blob.objects == {}


def test_company_page_without_company_id_is_a_problem(session, clock, listed):
    def bare(request):
        if request.url.path == "/company/VINATIORGA/consolidated/":
            return _html("no id here")

    server = FakeScreener(clock, override=bare)
    result = _adapter(server, COMPANIES[:2]).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "company id" in result.detail.lower()
    assert _count(session, ScreenerExport) == 1  # IOC still went through


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #


def test_never_faster_than_three_seconds_between_requests(session, clock, listed):
    server = FakeScreener(clock)
    result = _adapter(server).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    times = server.times()
    assert len(times) > 8
    assert all(b - a >= 3.0 for a, b in zip(times, times[1:], strict=False))


def test_setting_below_the_floor_is_raised_to_it(session, clock, listed):
    server = FakeScreener(clock)
    settings = _settings(screener_export_live_min_interval_seconds=0.1)
    _adapter(server, COMPANIES[:2]).run(_ctx(session, settings=settings))
    assert all(b - a >= 3.0 for a, b in zip(server.times(), server.times()[1:], strict=False))


def test_a_slower_setting_is_honoured(session, clock, listed):
    server = FakeScreener(clock)
    settings = _settings(screener_export_live_min_interval_seconds=7.0)
    _adapter(server, COMPANIES[:1]).run(_ctx(session, settings=settings))
    assert all(b - a >= 7.0 for a, b in zip(server.times(), server.times()[1:], strict=False))


# --------------------------------------------------------------------------- #
# Idempotence and ISIN resolution
# --------------------------------------------------------------------------- #


def test_rerun_stores_and_loads_nothing_new(session, clock, listed):
    server = FakeScreener(clock)
    blob = MemoryBlobStore()
    first = _adapter(server, COMPANIES[:1]).run(_ctx(session, blob=blob))
    assert first.status is RunStatus.SUCCEEDED, first.detail
    second = _adapter(server, COMPANIES[:1]).run(_ctx(session, now=NOW + timedelta(hours=2), blob=blob))
    assert second.status is RunStatus.SUCCEEDED, second.detail
    assert "1 files already loaded" in second.detail
    assert _count(session, RawSourceFile) == 1
    assert _count(session, ScreenerExport) == 1 and _count(session, ScreenerValue) == 431
    assert len(blob.objects) == 1


def test_changed_workbook_is_a_new_file_with_a_later_as_of(session, clock, listed):
    from io import BytesIO

    from openpyxl import load_workbook

    blob = MemoryBlobStore()
    _adapter(FakeScreener(clock), COMPANIES[:1]).run(_ctx(session, blob=blob))
    wb = load_workbook(FIXTURE)
    wb["Data Sheet"]["K17"] = 1700
    out = BytesIO()
    wb.save(out)
    later = NOW + timedelta(days=30)
    result = _adapter(FakeScreener(clock, export_body=out.getvalue()), COMPANIES[:1]).run(
        _ctx(session, now=later, blob=blob)
    )
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert _count(session, RawSourceFile) == 2
    assert [e.as_of for e in screener_exports_as_of(session, as_of=later, isin=VINATI)] == [NOW, later]


def test_unresolved_isin_is_quarantined_without_a_request(session, clock):
    _index_list(session, "VINATIORGA", VINATI)  # IOC is in no constituent list
    server = FakeScreener(clock)
    result = _adapter(server, COMPANIES[:2]).run(_ctx(session))
    assert result.status is RunStatus.FAILED and result.quarantined == 1
    assert "IOC" in result.detail
    assert not any("IOC" in p or "1002" in p for p in server.paths())
    assert _count(session, ScreenerExport) == 1


def test_symbol_listed_under_another_isin_is_quarantined(session, clock):
    _index_list(session, "VINATIORGA", VINATI)
    _index_list(session, "IOC", GAIL)  # the lists say IOC is a different ISIN than the sample's
    server = FakeScreener(clock)
    result = _adapter(server, COMPANIES[:2]).run(_ctx(session))
    assert result.quarantined == 1 and not any("IOC" in p for p in server.paths())


def test_default_companies_are_the_validation_sample():
    from validate.samples import CURRENT

    assert CURRENT.version == "validation-sample/1"
    adapter = ScreenerExportLive()
    assert [isin for isin, _ in adapter.companies()] == list(CURRENT.isins())
    assert dict(adapter.companies()) == {i: CURRENT.symbols[i] for i in CURRENT.isins()}


def test_workbook_dated_after_a_period_end_is_rejected_by_the_shared_loader(session, clock, listed):
    early = datetime(2020, 1, 1, 10, 0, tzinfo=IST)  # before the workbook's own periods end
    _index_list(session, "VINATIORGA", VINATI, published=datetime(2019, 12, 1, tzinfo=IST))
    result = _adapter(FakeScreener(clock), COMPANIES[:1]).run(_ctx(session, now=early))
    assert result.status is RunStatus.FAILED and "not before the export date" in result.detail
    assert _count(session, ScreenerExport) == 0


# --------------------------------------------------------------------------- #
# Credentials never leak
# --------------------------------------------------------------------------- #


def test_credentials_never_reach_logs_results_or_storage(session, clock, listed, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    server = FakeScreener(clock)
    blob = MemoryBlobStore()
    result = _adapter(server, COMPANIES[:1]).run(_ctx(session, blob=blob))
    failed = _adapter(FakeScreener(clock, login_ok=False), COMPANIES[:1]).run(_ctx(session))
    for text in (caplog.text, result.detail, failed.detail):
        assert PASSWORD not in text and USER not in text
    assert all(PASSWORD.encode() not in data and USER.encode() not in data for data in blob.objects.values())


def test_transport_error_message_does_not_echo_credentials(session, clock, listed, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(request):
        raise httpx.ConnectError("connection refused")

    adapter = ScreenerExportLive(transport=httpx.MockTransport(boom), companies=COMPANIES[:1],
                                 monotonic=clock.monotonic, sleep=clock.sleep)  # fmt: skip
    result = adapter.run(_ctx(session))
    assert result.status is RunStatus.FAILED
    assert PASSWORD not in result.detail and USER not in result.detail and PASSWORD not in caplog.text


def test_settings_never_print_the_password():
    settings = _settings()
    assert PASSWORD not in repr(settings) and PASSWORD not in str(settings.model_dump())
