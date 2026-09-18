"""Corporate-action price adjustment: pure, property-tested."""

from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.adjustment import (
    PriceFactor,
    adjust_series,
    bonus_factor,
    cumulative_factor,
    demerger_factor,
    dividend_factor,
    rights_factor,
    split_factor,
)

D = Decimal
START = date(2016, 1, 1)

prices = st.decimals(min_value=D("0.05"), max_value=D("100000"), places=2)
days = st.integers(0, 3650).map(lambda n: START + timedelta(days=n))
series = st.dictionaries(days, prices, min_size=1, max_size=30)
factors = st.builds(PriceFactor, days, st.decimals(min_value=D("0.01"), max_value=D("10"), places=4))


def test_split_and_consolidation_are_face_value_ratios():
    assert split_factor(D(10), D(2)) == D("0.2")
    assert split_factor(D(1), D(10)) == D(10)


def test_bonus_one_for_one_halves_and_three_for_two():
    assert bonus_factor(1, 1) == D("0.5")
    assert bonus_factor(3, 2) == D("0.4")


def test_rights_factor_is_terp_over_cum_close():
    # 1 new for 4 held at 60, cum close 100: TERP = (4*100 + 60)/5 = 92
    assert rights_factor(1, 4, D(60), D(100)) == D("0.92")


def test_rights_at_or_above_market_does_not_adjust():
    assert rights_factor(1, 4, D(100), D(100)) == D(1)
    assert rights_factor(1, 4, D(120), D(100)) == D(1)


def test_demerger_and_dividend_factors():
    assert demerger_factor(D("0.85")) == D("0.85")
    assert dividend_factor(D(5), D(100)) == D("0.95")


@pytest.mark.parametrize(
    "call",
    [
        lambda: split_factor(D(0), D(2)),
        lambda: bonus_factor(0, 1),
        lambda: rights_factor(1, 4, D(-1), D(100)),
        lambda: demerger_factor(D(1)),
        lambda: demerger_factor(D(0)),
        lambda: dividend_factor(D(100), D(100)),
        lambda: PriceFactor(START, D(0)),
    ],
)
def test_impossible_inputs_are_refused(call):
    with pytest.raises(ValueError):
        call()


@given(prices, st.sampled_from([(10, 2), (10, 1), (2, 1), (1, 10)]), days)
def test_a_split_leaves_no_jump_in_the_adjusted_series(close, fv, ex_date):
    """A constant-value company trades at close * factor after the split: adjusted, it is flat."""
    factor = split_factor(D(fv[0]), D(fv[1]))
    raw = {ex_date - timedelta(days=1): close, ex_date: close * factor}
    adjusted = adjust_series(raw, [PriceFactor(ex_date, factor)])
    assert adjusted[ex_date - timedelta(days=1)] == adjusted[ex_date]


@given(series, st.lists(factors, max_size=5))
def test_prices_on_or_after_every_ex_date_are_unchanged(s, fs):
    adjusted = adjust_series(s, fs)
    for day, price in s.items():
        if all(f.ex_date <= day for f in fs):
            assert adjusted[day] == price


@given(series, st.lists(factors, max_size=5), st.randoms())
def test_order_of_actions_does_not_matter(s, fs, rnd):
    shuffled = list(fs)
    rnd.shuffle(shuffled)
    assert adjust_series(s, fs) == adjust_series(s, shuffled)


@given(series, st.lists(factors, max_size=5), factors)
def test_an_action_after_the_whole_series_scales_everything_uniformly(s, fs, extra):
    later = PriceFactor(max(s) + timedelta(days=1), extra.factor)
    base, more = adjust_series(s, fs), adjust_series(s, [*fs, later])
    with localcontext(prec=80):  # exact, as in adjust_series; the default 28 digits would round
        assert all(more[d] == base[d] * extra.factor for d in s)


@given(series)
def test_no_actions_no_change(s):
    assert adjust_series(s, []) == s


def test_cumulative_factor_multiplies_only_later_ex_dates():
    fs = [PriceFactor(date(2020, 1, 1), D("0.5")), PriceFactor(date(2022, 1, 1), D("0.2"))]
    assert cumulative_factor(fs, date(2019, 12, 31)) == D("0.1")
    assert cumulative_factor(fs, date(2020, 1, 1)) == D("0.2")  # the ex-date itself is post-action
    assert cumulative_factor(fs, date(2022, 1, 1)) == D(1)
