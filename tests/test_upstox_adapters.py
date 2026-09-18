"""Upstox daily candle adapters against the documented response shape (no API app exists yet)."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from core.compute.hashing import content_hash
from core.compute.price_crosscheck import Mismatch, MismatchKind
from core.config import Settings
from core.db.models import (
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    NseBhavcopyRow,
    RawSourceFile,
    UpstoxCandle,
    UpstoxCandleQuarantine,
    UpstoxQuarantineReason,
)
from core.db.pit import (
    index_universe_isins_as_of,
    upstox_bhavcopy_crosscheck_as_of,
    upstox_candles_as_of,
    upstox_quarantine_review_as_of,
)
from core.timezones import IST
from ingest.base import AdapterContext, CanaryStatus, RunStatus
from ingest.upstox import parser as parser_module
from ingest.upstox.adapters import Tally, UpstoxDailyCandles, UpstoxDailyCandlesDrop, load_candles
from ingest.upstox.parser import RULE_VERSION, candles_url
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

BASE = "https://api.upstox.com/v3"
RELIANCE, INFY = "INE002A01018", "INE009A01021"
FETCHED = datetime(2026, 9, 16, 20, 0, tzinfo=IST)
LIST_PUBLISHED = datetime(2026, 9, 1, 23, 59, 59, tzinfo=IST)
DOCUMENTED = Path(__file__).parent / "fixtures" / "upstox" / (
    "historical_candle_NSE_EQ_INE002A01018_days_1_2026-09-16_2026-09-10.json"
)


def _body(*candles) -> bytes:
    return json.dumps({"status": "success", "data": {"candles": list(candles)}}).encode()


def _candle(day: str, close: float = 101.0) -> list:
    return [f"{day}T00:00:00+05:30", 100.0, 105.0, 95.0, close, 1000, 0]


class Web:
    """httpx.MockTransport handler: exact URLs; anything else is an empty success."""

    def __init__(self, pages: dict[str, tuple] | None = None) -> None:
        self.pages = {str(httpx.URL(url)): p for url, p in (pages or {}).items()}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, content = self.pages.get(str(request.url), (200, _body()))
        return httpx.Response(status, content=content, headers={"content-type": "application/json"})

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _url(isin: str, start: date, stop: date) -> str:
    return str(httpx.URL(candles_url(BASE, isin, start, stop)))


def _ctx(session, now: datetime = FETCHED, blob: MemoryBlobStore | None = None, **settings) -> AdapterContext:
    base = dict(
        upstox_min_interval_seconds=0,
        upstox_access_token=SecretStr("token-from-todays-login"),
        source_switches={"upstox_daily_candles": True, "upstox_daily_candles_drop": True},
    )
    return AdapterContext(session, blob or MemoryBlobStore(), Settings(**(base | settings)), now=lambda: now)


def _universe(session, *isins: str, as_of: datetime = LIST_PUBLISHED) -> None:
    prov = dict(
        as_of=as_of, content_hash=content_hash(b"list" + as_of.isoformat().encode()),
        source_url="https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
        extracted_by="tests", model_version=None,
    )  # fmt: skip
    snapshot = IndexSnapshot(
        index_code=IndexCode.NIFTY_50, constituent_count=len(isins), quarantined_rows=0, rule_version="t", **prov
    )
    session.add(snapshot)
    session.flush()
    session.add_all(
        IndexSnapshotConstituent(
            snapshot_id=snapshot.id, isin=isin, symbol=f"S{n}", series="EQ", company_name=f"C{n}",
            industry="I", row_number=n, **prov,
        )  # fmt: skip
        for n, isin in enumerate(isins, start=1)
    )
    session.flush()


def _all(session, model) -> list:
    return list(session.scalars(select(model).order_by(model.id)))


# --------------------------------------------------------------------------- #
# API adapter
# --------------------------------------------------------------------------- #


def test_first_run_walks_decades_per_isin_with_the_bearer_token(session):
    _universe(session, RELIANCE)
    current = _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16))
    web = Web({current: (200, DOCUMENTED.read_bytes())})
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session))

    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.raw_files, result.rows_written) == (3, 5)
    assert web.urls() == [
        _url(RELIANCE, date(2000, 1, 1), date(2009, 12, 31)),
        _url(RELIANCE, date(2010, 1, 1), date(2019, 12, 31)),
        current,
    ]  # no robots.txt: an official API, governed by its terms
    assert all(r.headers["authorization"] == "Bearer token-from-todays-login" for r in web.requests)

    candles = _all(session, UpstoxCandle)
    assert {c.as_of for c in candles} == {FETCHED}  # the vendor's view as of the fetch (R2)
    assert {c.instrument_key for c in candles} == {f"NSE_EQ|{RELIANCE}"}
    assert [r.source_url for r in _all(session, RawSourceFile)][-1] == current  # raw bytes kept first
    assert "token" not in " ".join(r.source_url for r in _all(session, RawSourceFile))


def test_rerun_skips_settled_decades_and_resumes_after_the_latest_candle(session):
    _universe(session, RELIANCE)
    first = Web({_url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16)): (200, DOCUMENTED.read_bytes())})
    UpstoxDailyCandles(first.transport()).run(_ctx(session))

    next_day = FETCHED + timedelta(days=1)
    web = Web({_url(RELIANCE, date(2026, 9, 17), date(2026, 9, 17)): (200, _body(_candle("2026-09-17")))})
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session, now=next_day))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert web.urls() == [_url(RELIANCE, date(2026, 9, 17), date(2026, 9, 17))]
    assert result.rows_written == 1

    same_evening = Web()
    UpstoxDailyCandles(same_evening.transport()).run(_ctx(session, now=next_day + timedelta(minutes=5)))
    assert same_evening.requests == []  # up to date: nothing left to ask for


def test_a_fetch_before_the_day_is_complete_asks_only_up_to_yesterday(session):
    _universe(session, RELIANCE)
    web = Web()
    UpstoxDailyCandles(web.transport()).run(_ctx(session, now=datetime(2026, 9, 16, 11, 0, tzinfo=IST)))
    assert web.urls()[-1] == _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 15))


def test_universe_is_every_isin_ever_listed_not_todays_list(session):
    _universe(session, RELIANCE, INFY, as_of=datetime(2016, 3, 10, 23, 59, 59, tzinfo=IST))
    _universe(session, RELIANCE)  # INFY since dropped (hypothetically): its history still matters
    assert index_universe_isins_as_of(session, as_of=FETCHED) == {RELIANCE, INFY}
    assert index_universe_isins_as_of(session, as_of=datetime(2016, 1, 1, tzinfo=IST)) == set()

    web = Web()
    UpstoxDailyCandles(web.transport()).run(_ctx(session))
    assert {parser_module.isin_from_url(u) for u in web.urls()} == {RELIANCE, INFY}


def test_missing_token_fails_without_calling_the_api(session):
    _universe(session, RELIANCE)
    web = Web()
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session, upstox_access_token=None))
    assert result.status is RunStatus.FAILED and "EQUITY_UPSTOX_ACCESS_TOKEN" in result.detail
    assert web.requests == []


def test_empty_universe_fails_loudly(session):
    result = UpstoxDailyCandles(Web().transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "no ISINs" in result.detail


def test_expired_token_stops_the_run_and_keeps_what_was_loaded(session):
    _universe(session, RELIANCE, INFY)
    reliance_current = _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16))
    web = Web({
        reliance_current: (200, DOCUMENTED.read_bytes()),
        _url(INFY, date(2000, 1, 1), date(2009, 12, 31)): (401, b'{"status":"error"}'),
    })  # fmt: skip
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "401" in result.detail
    assert web.urls()[-1] == _url(INFY, date(2000, 1, 1), date(2009, 12, 31))  # nothing after the block
    assert {c.isin for c in _all(session, UpstoxCandle)} == {RELIANCE}


def test_an_instrument_the_api_refuses_is_reported_and_the_rest_proceed(session):
    _universe(session, INFY, RELIANCE)  # sorted: INFY (INE009...) is asked for after RELIANCE (INE002...)
    web = Web({_url(RELIANCE, date(2000, 1, 1), date(2009, 12, 31)): (400, b'{"status":"error"}')})
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED and f"{RELIANCE} 2000-01-01..2009-12-31: HTTP 400" in result.detail
    assert sum(1 for u in web.urls() if RELIANCE in u) == 1
    assert sum(1 for u in web.urls() if INFY in u) == 3


def test_a_broken_response_is_quarantined_whole_and_its_bytes_kept(session):
    _universe(session, RELIANCE)
    current = _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16))
    web = Web({current: (200, b'{"status":"success","data":{"bars":[]}}')})
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    (q,) = _all(session, UpstoxCandleQuarantine)
    assert (q.reason, q.candle_index, q.source_url) == (UpstoxQuarantineReason.SHAPE_CHANGED, None, current)
    assert current in {r.source_url for r in _all(session, RawSourceFile)}


def test_newest_fetch_wins_per_day_without_rewriting_the_old_view(session):
    _universe(session, RELIANCE)
    url = _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16))
    UpstoxDailyCandles(Web({url: (200, _body(_candle("2026-09-16", close=101.0)))}).transport()).run(_ctx(session))

    # The vendor later shows a different close for the same day (e.g. an adjustment): a
    # drop-folder copy fetched later is a new view, stored beside the old one.
    later = FETCHED + timedelta(days=30)
    drop = UpstoxDailyCandlesDrop()
    ctx = _ctx(session, now=later)
    doc = drop.store_raw(ctx, data=_body(_candle("2026-09-16", close=97.5)), source_url=url, as_of=later,
                         media_type="application/json")  # fmt: skip
    load_candles(drop, ctx, doc, Tally())

    day = date(2026, 9, 16)
    (before,) = upstox_candles_as_of(session, isin=RELIANCE, start=day, end=day, as_of=later - timedelta(seconds=1))
    (after,) = upstox_candles_as_of(session, isin=RELIANCE, start=day, end=day, as_of=later)
    assert (before.close, after.close) == (Decimal("101.0000"), Decimal("97.5000"))
    assert upstox_candles_as_of(session, isin=RELIANCE, start=day, end=day, as_of=FETCHED - timedelta(seconds=1)) == []


def test_crosscheck_reports_where_upstox_differs_from_the_exchange(session):
    _universe(session, RELIANCE)
    url = _url(RELIANCE, date(2020, 1, 1), date(2026, 9, 16))
    body = _body(_candle("2026-09-14"), _candle("2026-09-15", close=97.5), _candle("2026-09-16"))
    UpstoxDailyCandles(Web({url: (200, body)}).transport()).run(_ctx(session))
    for day in (date(2026, 9, 15), date(2026, 9, 16)):  # bhavcopy has no 14th loaded: outside the overlap
        session.add(
            NseBhavcopyRow(
                trade_date=day, isin=RELIANCE, symbol="RELIANCE", series="EQ",
                open=Decimal("100.00"), high=Decimal("105.00"), low=Decimal("95.00"), close=Decimal("101.00"),
                prev_close=Decimal("100.00"), volume=1000, turnover=Decimal("101000.00"), trades=10,
                rule_version="t", as_of=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=IST),
                content_hash=content_hash(day.isoformat().encode()), source_url=f"bhav-{day}",
                extracted_by="tests", model_version=None,
            )  # fmt: skip
        )
    session.flush()

    found = upstox_bhavcopy_crosscheck_as_of(
        session, isin=RELIANCE, start=date(2026, 9, 1), end=date(2026, 9, 30), as_of=FETCHED
    )
    assert found == [
        Mismatch(date(2026, 9, 15), MismatchKind.PRICE_DIFFERS, "close", Decimal("97.5000"), Decimal("101.0000"))
    ]
    before_upstox = FETCHED - timedelta(seconds=1)
    assert upstox_bhavcopy_crosscheck_as_of(
        session, isin=RELIANCE, start=date(2026, 9, 1), end=date(2026, 9, 30), as_of=before_upstox
    ) == []


def test_canary_validates_a_recent_window_for_one_known_isin(session):
    end = date(2026, 9, 16)
    url = _url(RELIANCE, end - timedelta(days=14), end)
    assert UpstoxDailyCandles(Web({url: (200, DOCUMENTED.read_bytes())}).transport()).canary(
        _ctx(session)
    ).status is CanaryStatus.PASSED
    with pytest.raises(RuntimeError, match="no candles"):
        UpstoxDailyCandles(Web().transport()).canary(_ctx(session))


def test_disabled_adapter_writes_nothing(session):
    _universe(session, RELIANCE)
    web = Web()
    result = UpstoxDailyCandles(web.transport()).run(_ctx(session, source_switches={}))
    assert result.status is RunStatus.DISABLED and web.requests == []


# --------------------------------------------------------------------------- #
# Drop folder and reparse
# --------------------------------------------------------------------------- #


def _drop(tmp_path: Path, name: str, data: bytes, source_url: str, published_at: str) -> None:
    folder = tmp_path / "upstox_daily_candles_drop"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data)
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": source_url, "published_at": published_at, "media_type": "application/json"})
    )


def test_drop_folder_loads_a_saved_response_once(session, tmp_path):
    url = f"{BASE}/historical-candle/NSE_EQ%7C{RELIANCE}/days/1/2026-09-16/2026-09-10"
    _drop(tmp_path, "reliance.json", DOCUMENTED.read_bytes(), url, "2026-09-16T20:00:00+05:30")
    adapter = UpstoxDailyCandlesDrop()
    first = adapter.run(_ctx(session, drop_folder=tmp_path))
    assert first.status is RunStatus.SUCCEEDED and first.rows_written == 5, first.detail
    again = adapter.run(_ctx(session, drop_folder=tmp_path, now=FETCHED + timedelta(hours=1)))
    assert (again.rows_written, again.status) == (0, RunStatus.SUCCEEDED)
    assert len(_all(session, UpstoxCandle)) == 5


def test_drop_file_whose_url_names_no_isin_is_quarantined(session, tmp_path):
    _drop(tmp_path, "x.json", DOCUMENTED.read_bytes(), "https://example.com/x.json", "2026-09-16T20:00:00+05:30")
    result = UpstoxDailyCandlesDrop().run(_ctx(session, drop_folder=tmp_path))
    assert result.status is RunStatus.FAILED
    (q,) = _all(session, UpstoxCandleQuarantine)
    assert q.reason is UpstoxQuarantineReason.UNKNOWN_INSTRUMENT


def test_reparse_under_a_fixed_rule_resolves_the_quarantine_from_stored_bytes(session, tmp_path, monkeypatch):
    url = f"{BASE}/historical-candle/NSE_EQ%7C{RELIANCE}/days/1/2026-09-16/2026-09-10"
    six_fields = _body(["2026-09-16T00:00:00+05:30", 100.0, 105.0, 95.0, 101.0, 1000])
    _drop(tmp_path, "r.json", six_fields, url, "2026-09-16T20:00:00+05:30")
    blob = MemoryBlobStore()
    adapter = UpstoxDailyCandlesDrop()
    adapter.run(_ctx(session, drop_folder=tmp_path, blob=blob))
    (entry,) = upstox_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=FETCHED)
    assert entry.needs_review and entry.row.reason is UpstoxQuarantineReason.MALFORMED_CANDLE
    assert entry.row.trade_date is None  # nothing to match by date: matched by position instead

    # Hypothetical rule fix: Upstox turns out to omit open interest for equities.
    real_parse = parser_module.parse_candles

    def fixed_parse(data, *, last_complete):
        payload = json.loads(data)
        payload["data"]["candles"] = [c + [0] if len(c) == 6 else c for c in payload["data"]["candles"]]
        return real_parse(json.dumps(payload).encode(), last_complete=last_complete)

    monkeypatch.setattr("ingest.upstox.adapters.parse_candles", fixed_parse)
    monkeypatch.setattr("ingest.upstox.adapters.RULE_VERSION", "upstox_candles/test-fix")
    result = adapter.reparse(_ctx(session, drop_folder=tmp_path, blob=blob, now=FETCHED + timedelta(days=1)))
    assert result.rows_written == 1, result.detail
    (entry,) = upstox_quarantine_review_as_of(session, current_rule_version="upstox_candles/test-fix", as_of=FETCHED)
    assert entry.resolved
    (still,) = upstox_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=FETCHED)
    assert still.needs_review  # judged against the old rule, nothing has changed
    (candle,) = _all(session, UpstoxCandle)
    assert candle.as_of == FETCHED  # a reparse keeps the response's own fetch time


def test_a_reparse_that_quarantines_the_same_response_again_leaves_it_for_review(session, tmp_path, monkeypatch):
    url = f"{BASE}/historical-candle/NSE_EQ%7C{RELIANCE}/days/1/2026-09-16/2026-09-10"
    _drop(tmp_path, "r.json", b'{"status":"success","data":{"bars":[]}}', url, "2026-09-16T20:00:00+05:30")
    blob = MemoryBlobStore()
    adapter = UpstoxDailyCandlesDrop()
    adapter.run(_ctx(session, drop_folder=tmp_path, blob=blob))

    monkeypatch.setattr("ingest.upstox.adapters.RULE_VERSION", "upstox_candles/test-fix")
    result = adapter.reparse(_ctx(session, drop_folder=tmp_path, blob=blob, now=FETCHED + timedelta(days=1)))
    assert result.status is RunStatus.FAILED
    entries = upstox_quarantine_review_as_of(session, current_rule_version="upstox_candles/test-fix", as_of=FETCHED)
    assert [(e.row.rule_version, e.needs_review) for e in entries] == [
        (RULE_VERSION, True), ("upstox_candles/test-fix", True),
    ]  # fmt: skip
