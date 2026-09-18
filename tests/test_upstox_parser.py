"""Upstox v3 daily candle parser, against the documented response shape (tests/fixtures/upstox/SOURCES.md)."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.db.models import UpstoxQuarantineReason
from core.timezones import IST
from ingest.upstox.parser import (
    CandlesRejected,
    candles_url,
    isin_from_url,
    last_complete_day,
    parse_candles,
    request_windows,
    require_isin,
)

FIXTURES = Path(__file__).parent / "fixtures" / "upstox"
DOCUMENTED = FIXTURES / "historical_candle_NSE_EQ_INE002A01018_days_1_2026-09-16_2026-09-10.json"
RELIANCE = "INE002A01018"
BASE = "https://api.upstox.com/v3"
LATE = date(2026, 12, 31)


def _body(*candles, **envelope) -> bytes:
    return json.dumps({"status": "success", "data": {"candles": list(candles)}} | envelope).encode()


def _candle(day: str = "2026-09-16", o=100.5, h=102, lo=99.25, c=101, vol=1000, oi=0) -> list:
    return [f"{day}T00:00:00+05:30", o, h, lo, c, vol, oi]


def test_documented_response_parses_oldest_first_with_exact_decimals():
    parsed = parse_candles(DOCUMENTED.read_bytes(), last_complete=LATE)
    assert parsed.issues == ()
    assert [c.trade_date for c in parsed.candles] == [
        date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16),
    ]  # fmt: skip
    newest = parsed.candles[-1]
    assert (newest.open, newest.high, newest.low, newest.close) == (
        Decimal("1398.7"), Decimal("1412.4"), Decimal("1391.05"), Decimal("1409.9"),
    )  # fmt: skip
    assert all(isinstance(v, Decimal) for c in parsed.candles for v in (c.open, c.high, c.low, c.close))
    assert parsed.candles[3].open == Decimal("1385")  # a JSON integer price
    assert (newest.volume, newest.open_interest) == (8123456, 0)


def test_price_is_never_routed_through_float():
    # 0.1 + 0.2 style values must survive exactly; float would give 1409.8999999...
    parsed = parse_candles(_body(_candle(o=1409.9, h=1409.9, lo=1409.9, c=1409.9)), last_complete=LATE)
    assert parsed.candles[0].close == Decimal("1409.9")


@pytest.mark.parametrize(
    "candle, fragment",
    [
        (_candle()[:6], "6 fields"),
        (_candle(o="100.5"), "open"),
        (_candle(vol=10.5), "volume"),
        (_candle(vol=-1), "volume"),
        (_candle(o=-1), "open"),
        (_candle(o=True), "open"),
        (["2026-09-16T09:15:00+05:30", 1, 1, 1, 1, 1, 0], "timestamp"),
        (["2026-09-16T00:00:00Z", 1, 1, 1, 1, 1, 0], "timestamp"),
        (["2026-02-30T00:00:00+05:30", 1, 1, 1, 1, 1, 0], "timestamp"),
    ],
)
def test_malformed_candle_is_quarantined_alone(candle, fragment):
    parsed = parse_candles(_body(candle, _candle("2026-09-15")), last_complete=LATE)
    assert [c.trade_date for c in parsed.candles] == [date(2026, 9, 15)]
    (issue,) = parsed.issues
    assert issue.reason is UpstoxQuarantineReason.MALFORMED_CANDLE
    assert fragment in issue.detail and issue.candle_index == 0
    assert issue.raw_candle.startswith("[")


def test_inconsistent_ohlc_is_quarantined():
    parsed = parse_candles(_body(_candle(o=105, h=102)), last_complete=LATE)
    assert parsed.candles == ()
    assert parsed.issues[0].reason is UpstoxQuarantineReason.OHLC_INCONSISTENT
    assert parsed.issues[0].trade_date == date(2026, 9, 16)


def test_two_bars_for_one_day_are_both_quarantined():
    parsed = parse_candles(
        _body(_candle(c=101), _candle("2026-09-15"), _candle(c=100)), last_complete=LATE
    )
    assert [c.trade_date for c in parsed.candles] == [date(2026, 9, 15)]
    assert [(i.candle_index, i.reason) for i in parsed.issues] == [
        (0, UpstoxQuarantineReason.DUPLICATE_DATE), (2, UpstoxQuarantineReason.DUPLICATE_DATE),
    ]  # fmt: skip


def test_a_bar_for_a_day_not_yet_complete_is_quarantined():
    parsed = parse_candles(_body(_candle("2026-09-16"), _candle("2026-09-15")), last_complete=date(2026, 9, 15))
    assert [c.trade_date for c in parsed.candles] == [date(2026, 9, 15)]
    assert "last complete day" in parsed.issues[0].detail


def test_empty_candle_list_is_a_valid_response():
    assert parse_candles(_body(), last_complete=LATE).candles == ()


@pytest.mark.parametrize(
    "data",
    [
        b"<html>maintenance</html>",
        b'{"status":"error","errors":[{"errorCode":"UDAPI1021"}]}',
        _body(_candle(), extra="field"),
        json.dumps({"status": "success", "data": {"candles": [], "next": 1}}).encode(),
        json.dumps({"status": "success", "data": {"bars": []}}).encode(),
    ],
)
def test_changed_shape_rejects_the_whole_response(data):
    with pytest.raises(CandlesRejected) as exc:
        parse_candles(data, last_complete=LATE)
    assert exc.value.reason is UpstoxQuarantineReason.SHAPE_CHANGED


def test_url_is_keyed_by_isin_and_round_trips():
    url = candles_url(BASE + "/", RELIANCE, date(2000, 1, 1), date(2009, 12, 31))
    assert url == f"{BASE}/historical-candle/NSE_EQ%7C{RELIANCE}/days/1/2009-12-31/2000-01-01"
    assert isin_from_url(url) == require_isin(url) == RELIANCE
    assert isin_from_url(f"{BASE}/historical-candle/NSE_EQ|{RELIANCE}/days/1/2009-12-31/2000-01-01") == RELIANCE
    assert isin_from_url(f"{BASE}/historical-candle/NSE_EQ%7C{RELIANCE}/weeks/1/2009-12-31") is None


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/historical-candle/BSE_EQ%7C{RELIANCE}/days/1/2026-09-16/2026-09-10",
        f"{BASE}/historical-candle/NSE_EQ%7CINE002A01019/days/1/2026-09-16/2026-09-10",  # bad check digit
        "https://example.com/candles.json",
    ],
)
def test_response_without_a_valid_nse_isin_is_rejected(url):
    with pytest.raises(CandlesRejected) as exc:
        require_isin(url)
    assert exc.value.reason is UpstoxQuarantineReason.UNKNOWN_INSTRUMENT


def test_request_windows_are_fixed_decades_from_2000():
    assert request_windows(date(2026, 9, 16), None) == [
        (date(2000, 1, 1), date(2009, 12, 31)),
        (date(2010, 1, 1), date(2019, 12, 31)),
        (date(2020, 1, 1), date(2026, 9, 16)),
    ]


def test_request_windows_resume_after_the_latest_stored_candle():
    assert request_windows(date(2026, 9, 16), date(2026, 9, 11)) == [(date(2026, 9, 12), date(2026, 9, 16))]
    assert request_windows(date(2026, 9, 16), date(2015, 6, 30)) == [
        (date(2015, 7, 1), date(2019, 12, 31)),
        (date(2020, 1, 1), date(2026, 9, 16)),
    ]
    assert request_windows(date(2026, 9, 16), date(2026, 9, 16)) == []
    assert request_windows(date(2019, 12, 31), date(2009, 12, 31)) == [(date(2010, 1, 1), date(2019, 12, 31))]


@pytest.mark.parametrize(
    "fetched, expected",
    [
        (datetime(2026, 9, 16, 17, 59, 59, tzinfo=IST), date(2026, 9, 15)),
        (datetime(2026, 9, 16, 18, 0, tzinfo=IST), date(2026, 9, 16)),
        (datetime(2026, 9, 16, 13, 0, tzinfo=IST), date(2026, 9, 15)),  # mid-session: today is partial
        # 20:00 UTC on the 15th is 01:30 IST on the 16th: the 15th is complete, the 16th is not
        (datetime.fromisoformat("2026-09-15T20:00:00+00:00"), date(2026, 9, 15)),
    ],
)
def test_last_complete_day_uses_ist(fetched, expected):
    assert last_complete_day(fetched) == expected


def test_last_complete_day_rejects_naive_time():
    with pytest.raises(ValueError):
        last_complete_day(datetime(2026, 9, 16, 20, 0))
