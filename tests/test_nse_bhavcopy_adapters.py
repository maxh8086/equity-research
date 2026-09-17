"""NSE bhavcopy adapters against small synthetic files (real-shaped, tiny row counts)."""

import json
from datetime import date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from core.config import Settings
from core.db.models import BhavcopyQuarantineReason, NseBhavcopyQuarantine, NseBhavcopyRow
from core.db.pit import bhavcopy_quarantine_review_as_of, symbol_to_isin_as_of
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_bhavcopy import adapters
from ingest.nse_bhavcopy import parser as parser_module
from ingest.nse_bhavcopy.adapters import NseBhavcopy, NseBhavcopyDrop
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

TEMPLATE = "https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
DAY1, DAY2, DAY3 = date(2024, 7, 8), date(2024, 7, 9), date(2024, 7, 10)
FETCHED = datetime(2024, 7, 10, 20, 0, tzinfo=IST)

RELIANCE_ISIN = "INE002A01018"
HEADER = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
    "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
    "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,"
    "Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
)  # fmt: skip


def _row(trade_date: str, isin: str = RELIANCE_ISIN, symbol: str = "RELIANCE", series: str = "EQ") -> str:
    return (
        f"{trade_date},{trade_date},CM,NSE,STK,1,{isin},{symbol},{series},,,,,{symbol} LTD,"
        "100.00,105.00,95.00,101.00,101.00,99.00,,101.00,,,1000,100000.00,10,F1,1,,,,,"
    )


def _bhavcopy(trade_date: str, *extra_rows: str) -> bytes:
    return ("\n".join([HEADER, _row(trade_date), *extra_rows]) + "\n").encode()


def _url(day: date) -> str:
    return TEMPLATE.format(date=day.strftime("%Y%m%d"))


class Web:
    """httpx.MockTransport handler: exact URLs, defaulting to 404 (CLAUDE.md "Breakage":
    NSE's archive simply has no file for a non-trading day, this is not a block)."""

    def __init__(self, pages: dict[str, tuple] | None = None) -> None:
        self.pages = {str(httpx.URL(url)): p for url, p in (pages or {}).items()}
        self.requests: list[httpx.Request] = []

    def set(self, url: str, p: tuple) -> None:
        self.pages[str(httpx.URL(url))] = p

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, content, headers = self.pages.get(str(request.url), page(status=404))
        return httpx.Response(status, content=content, headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def page(content: bytes = b"", status: int = 200, headers: dict | None = None) -> tuple:
    return status, content, headers or {"content-type": "application/zip"}


@pytest.fixture(autouse=True)
def _tiny_bounds(monkeypatch):
    """These tests exercise the adapter's day-walk and storage, not the parser's sanity
    bands (tests/test_nse_bhavcopy_parser.py covers those on the real recorded file)."""
    monkeypatch.setattr(parser_module, "MIN_TOTAL_ROWS", 1)
    monkeypatch.setattr(parser_module, "MIN_EQUITY_ROWS", 1)
    monkeypatch.setattr(parser_module, "MAX_EQUITY_ROWS", 10)


def _ctx(session, now: datetime = FETCHED, blob: MemoryBlobStore | None = None, **settings) -> AdapterContext:
    base = dict(
        nse_bhavcopy_min_interval_seconds=0,
        source_switches={"nse_bhavcopy": True, "nse_bhavcopy_drop": True},
    )
    return AdapterContext(session, blob or MemoryBlobStore(), Settings(**(base | settings)), now=lambda: now)


def _all(session, model) -> list:
    return list(session.scalars(select(model)))


# --------------------------------------------------------------------------- #
# Archive adapter: day-by-day walk
# --------------------------------------------------------------------------- #


def test_ingest_walks_forward_storing_rows_and_skipping_holidays(session):
    web = Web({_url(DAY1): page(_bhavcopy("2024-07-08")), _url(DAY3): page(_bhavcopy("2024-07-10"))})
    result = NseBhavcopy(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.raw_files, result.rows_written) == (2, 2)
    assert [str(r.url) for r in web.requests] == [
        str(httpx.URL("https://archives.nseindia.com/robots.txt")),
        *[str(httpx.URL(_url(d))) for d in (DAY1, DAY2, DAY3)],
    ]

    rows = {r.trade_date: r for r in _all(session, NseBhavcopyRow)}
    assert set(rows) == {DAY1, DAY3}
    assert rows[DAY1].as_of == datetime(2024, 7, 8, 23, 59, 59, tzinfo=IST)  # backfilled: true day-end
    assert rows[DAY3].as_of == FETCHED  # same-day fetch: capped, never claims later knowledge than we have (R2)

    # Rerun: the resume cursor starts after the latest loaded date, so nothing is refetched
    # -- not even a holiday's 404, which a rerun would otherwise re-check forever.
    web2 = Web()
    rerun = NseBhavcopy(web2.transport()).run(_ctx(session, now=FETCHED + timedelta(hours=1)))
    assert rerun.status is RunStatus.SUCCEEDED, rerun.detail
    assert web2.requests == []
    assert len(_all(session, NseBhavcopyRow)) == 2


def test_rerun_only_rechecks_the_current_holiday_streak(session):
    web1 = Web({_url(DAY1): page(_bhavcopy("2024-07-08"))})
    NseBhavcopy(web1.transport()).run(_ctx(session, now=datetime(2024, 7, 8, 20, 0, tzinfo=IST)))

    web2 = Web({_url(DAY3): page(_bhavcopy("2024-07-10"))})  # DAY2 stays a holiday
    result = NseBhavcopy(web2.transport()).run(_ctx(session, now=datetime(2024, 7, 10, 20, 0, tzinfo=IST)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert {r.trade_date for r in _all(session, NseBhavcopyRow)} == {DAY1, DAY3}

    fetched = {str(r.url) for r in web2.requests if "robots.txt" not in str(r.url)}
    assert fetched == {str(httpx.URL(_url(DAY2))), str(httpx.URL(_url(DAY3)))}  # DAY1 never rechecked


def test_symbol_resolves_to_the_isin_it_traded_under_on_that_date(session):
    web = Web({_url(DAY1): page(_bhavcopy("2024-07-08"))})
    NseBhavcopy(web.transport()).run(_ctx(session, now=datetime(2024, 7, 8, 20, 0, tzinfo=IST)))
    assert symbol_to_isin_as_of(session, symbol="RELIANCE", on=DAY1, as_of=FETCHED) == RELIANCE_ISIN
    assert symbol_to_isin_as_of(session, symbol="RELIANCE", on=DAY2, as_of=FETCHED) == RELIANCE_ISIN
    assert symbol_to_isin_as_of(session, symbol="RELIANCE", on=date(2024, 7, 7), as_of=FETCHED) is None
    assert symbol_to_isin_as_of(session, symbol="NOSUCHSYMBOL", on=DAY3, as_of=FETCHED) is None


def test_blocked_source_stops_the_run_and_keeps_what_was_loaded(session):
    web = Web({_url(DAY1): page(_bhavcopy("2024-07-08")), _url(DAY2): page(status=403)})
    result = NseBhavcopy(web.transport()).run(_ctx(session, now=datetime(2024, 7, 9, 20, 0, tzinfo=IST)))
    assert result.status is RunStatus.FAILED and "403" in result.detail
    assert [r.trade_date for r in _all(session, NseBhavcopyRow)] == [DAY1]


def test_check_shape_finds_the_latest_available_day_without_storing(session):
    web = Web({_url(DAY1): page(_bhavcopy("2024-07-08"))})
    NseBhavcopy(web.transport()).check_shape(_ctx(session, now=datetime(2024, 7, 12, 10, 0, tzinfo=IST)))
    assert _all(session, NseBhavcopyRow) == []


def test_check_shape_raises_when_nothing_found_in_ten_days(session):
    with pytest.raises(RuntimeError):
        NseBhavcopy(Web().transport()).check_shape(_ctx(session, now=datetime(2024, 7, 12, 10, 0, tzinfo=IST)))


# --------------------------------------------------------------------------- #
# Quarantine and reparse
# --------------------------------------------------------------------------- #


def test_whole_file_quarantine_is_resolved_by_reparse_after_bounds_relax(session, monkeypatch):
    blob = MemoryBlobStore()
    tiny = _bhavcopy("2024-07-08")  # exactly one equity row
    now1 = datetime(2024, 7, 8, 20, 0, tzinfo=IST)

    monkeypatch.setattr(parser_module, "MIN_EQUITY_ROWS", 5)  # stricter than this file has
    result = NseBhavcopy(Web({_url(DAY1): page(tiny)}).transport()).run(_ctx(session, now=now1, blob=blob))
    assert result.status is RunStatus.FAILED
    [q] = _all(session, NseBhavcopyQuarantine)
    assert (q.reason, q.row_number, q.trade_date) == (BhavcopyQuarantineReason.ROW_COUNT, None, None)

    [before] = bhavcopy_quarantine_review_as_of(session, as_of=now1)
    assert before.needs_review and before.row.id == q.id

    monkeypatch.setattr(parser_module, "MIN_EQUITY_ROWS", 1)  # the bound is relaxed
    monkeypatch.setattr(adapters, "RULE_VERSION", "nse_bhavcopy/test-relaxed")
    later = now1 + timedelta(hours=1)
    reparsed = NseBhavcopy(Web().transport()).reparse(_ctx(session, now=later, blob=blob))
    assert reparsed.status is RunStatus.SUCCEEDED, reparsed.detail

    [row] = _all(session, NseBhavcopyRow)
    assert row.trade_date == DAY1 and row.as_of == now1  # true original as_of, never today's

    [after] = bhavcopy_quarantine_review_as_of(session, as_of=later)
    assert (after.row.id, after.resolved, after.needs_review) == (q.id, True, False)


def test_row_quarantine_keeps_the_rest_of_the_file(session):
    bad_isin = RELIANCE_ISIN[:-1] + str((int(RELIANCE_ISIN[-1]) + 1) % 10)
    bad = _bhavcopy("2024-07-08", _row("2024-07-08", isin=bad_isin, symbol="TCS"))
    result = NseBhavcopy(Web({_url(DAY1): page(bad)}).transport()).run(
        _ctx(session, now=datetime(2024, 7, 8, 20, 0, tzinfo=IST))
    )
    assert result.status is RunStatus.SUCCEEDED and result.quarantined == 1  # the rest of the file loads
    [row] = _all(session, NseBhavcopyRow)
    assert row.symbol == "RELIANCE"
    [q] = [r for r in _all(session, NseBhavcopyQuarantine) if r.row_number is not None]
    assert (q.reason, q.symbol, q.trade_date) == (BhavcopyQuarantineReason.INVALID_ISIN, "TCS", DAY1)


def test_reparse_of_an_already_current_file_changes_nothing(session):
    blob = MemoryBlobStore()
    web = Web({_url(DAY1): page(_bhavcopy("2024-07-08"))})
    NseBhavcopy(web.transport()).run(_ctx(session, now=datetime(2024, 7, 8, 20, 0, tzinfo=IST), blob=blob))
    result = NseBhavcopy(Web().transport()).reparse(_ctx(session, now=FETCHED, blob=blob))
    assert result.status is RunStatus.SUCCEEDED and "1 already loaded" in result.detail
    assert len(_all(session, NseBhavcopyRow)) == 1


# --------------------------------------------------------------------------- #
# Drop folder
# --------------------------------------------------------------------------- #


def test_dropped_bhavcopy_is_dated_by_its_sidecar_and_loaded_once(session, tmp_path):
    folder = tmp_path / NseBhavcopyDrop.name
    folder.mkdir()
    (folder / "bhav_20240708.csv").write_bytes(_bhavcopy("2024-07-08"))
    meta = {
        "source_url": _url(DAY1),
        "published_at": "2024-07-08T23:59:59+05:30",
        "media_type": "text/csv",
    }
    (folder / "bhav_20240708.csv.meta.json").write_text(json.dumps(meta))

    result = NseBhavcopyDrop().run(_ctx(session, drop_folder=tmp_path))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    rerun = NseBhavcopyDrop().run(_ctx(session, now=FETCHED + timedelta(hours=1), drop_folder=tmp_path))
    assert rerun.status is RunStatus.SUCCEEDED and "1 already loaded" in rerun.detail

    [row] = _all(session, NseBhavcopyRow)
    assert (row.trade_date, row.isin, row.as_of) == (DAY1, RELIANCE_ISIN, datetime(2024, 7, 8, 23, 59, 59, tzinfo=IST))
