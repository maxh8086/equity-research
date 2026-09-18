"""Vendor vs. exchange daily bar cross-check: pure, property-tested."""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from core.compute.price_crosscheck import PRICE_TOLERANCE, Bar, Mismatch, MismatchKind, crosscheck

D1, D2, D3 = date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)


def _bar(close: str = "101.00", volume: int = 1000) -> Bar:
    return Bar(Decimal("100.00"), Decimal("105.00"), Decimal("95.00"), Decimal(close), volume)


prices = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("100000"), places=2)
bars = st.builds(
    lambda o, h, lo, c, v: Bar(o, max(o, h, lo, c), min(o, h, lo, c), c, v),
    prices, prices, prices, prices, st.integers(min_value=0, max_value=10**10),
)  # fmt: skip
series = st.dictionaries(st.integers(0, 3650).map(lambda n: date(2016, 1, 1) + timedelta(days=n)), bars, max_size=30)


@given(series)
def test_a_series_agrees_with_itself(s):
    assert crosscheck(s, dict(s)) == []


@given(series, st.decimals(min_value=Decimal("0.02"), max_value=Decimal("1000"), places=2))
def test_every_close_moved_beyond_tolerance_is_reported(s, shift):
    shifted = {d: Bar(b.open, b.high + shift, b.low, b.close + shift, b.volume) for d, b in s.items()}
    found = {(m.trade_date, m.field) for m in crosscheck(shifted, s) if m.kind is MismatchKind.PRICE_DIFFERS}
    assert found == {(d, f) for d in s for f in ("high", "close")}


@given(series)
def test_a_one_paisa_difference_is_tolerated(s):
    nudged = {d: Bar(b.open, b.high, b.low, b.close + PRICE_TOLERANCE, b.volume) for d, b in s.items()}
    assert crosscheck(nudged, s) == []


def test_findings_name_the_day_the_field_and_both_values():
    vendor = {D1: _bar(), D2: _bar(close="50.50"), D3: _bar(volume=999)}
    exchange = {D1: _bar(), D2: _bar(), D3: _bar()}
    assert crosscheck(vendor, exchange) == [
        Mismatch(D2, MismatchKind.PRICE_DIFFERS, "close", Decimal("50.50"), Decimal("101.00")),
        Mismatch(D3, MismatchKind.VOLUME_DIFFERS, "volume", 999, 1000),
    ]


def test_a_day_on_one_side_only_is_reported_as_missing_on_the_other():
    assert crosscheck({D1: _bar(), D3: _bar()}, {D1: _bar(), D2: _bar()}) == [
        Mismatch(D2, MismatchKind.MISSING_AT_VENDOR),
        Mismatch(D3, MismatchKind.MISSING_AT_EXCHANGE),
    ]
