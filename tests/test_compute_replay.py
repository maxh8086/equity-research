from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.replay import (
    DEFAULT_HIT_THRESHOLD_PCT,
    Gate,
    GateOutcome,
    GateResult,
    KnownState,
    LookAhead,
    NotYetObservable,
    Outcome,
    OverlappingWindows,
    Raised,
    ReturnBasis,
    ReviewAction,
    Signal,
    SignalCategory,
    Window,
    hit_rates,
    measure,
    raise_on,
    replay,
)

ISIN = "INE848E01016"
D0 = date(2022, 1, 3)


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def sig(
    category: SignalCategory,
    action: ReviewAction = ReviewAction.ADD_REVIEW,
    on: date = D0,
    isin: str = ISIN,
) -> Signal:
    return Signal(isin, on, category, action, "rule/1", f"https://example.test/{category.value}")


def state(**kw) -> KnownState:
    base = dict(
        as_of=D0,
        close=Decimal("100"),
        base_value=Decimal("120"),
        thesis_intact=True,
        weight_pct=Decimal("4"),
        cap_pct=Decimal("8"),
    )
    return KnownState(**{**base, **kw})


def gate(r: Raised, g: Gate) -> GateOutcome:
    return next(x.outcome for x in r.gates if x.gate is g)


# --- gates ------------------------------------------------------------------


def test_two_categories_below_base_value_thesis_intact_and_within_cap_raises():
    (r,) = raise_on(
        ISIN, D0, [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.CREDIT_RATING)], state()
    )
    assert r.action is ReviewAction.ADD_REVIEW
    assert r.would_have_raised
    assert r.blocked_by == ()
    assert r.categories == (SignalCategory.CREDIT_RATING, SignalCategory.OWNERSHIP)
    assert len(r.evidence_urls) == 2


def test_one_category_is_blocked_but_still_reported_with_the_gate_that_blocked_it():
    (r,) = raise_on(ISIN, D0, [sig(SignalCategory.OWNERSHIP)], state())
    assert not r.would_have_raised
    assert r.blocked_by == (Gate.TWO_INDEPENDENT_CATEGORIES,)


def test_two_signals_in_one_category_do_not_satisfy_the_independence_gate():
    signals = [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.OWNERSHIP)]
    (r,) = raise_on(ISIN, D0, signals, state())
    assert r.categories == (SignalCategory.OWNERSHIP,)
    assert r.blocked_by == (Gate.TWO_INDEPENDENT_CATEGORIES,)


def test_price_at_or_above_the_base_value_blocks():
    signals = [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.EXPANSION)]
    (r,) = raise_on(ISIN, D0, signals, state(close=Decimal("120")))
    assert r.blocked_by == (Gate.PRICE_BELOW_BASE_OR_BULL,)


def test_bull_value_is_used_only_when_no_base_value_was_computed():
    signals = [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.EXPANSION)]
    (r,) = raise_on(
        ISIN, D0, signals, state(base_value=None, bull_value=Decimal("150"), close=Decimal("140"))
    )
    assert gate(r, Gate.PRICE_BELOW_BASE_OR_BULL) is GateOutcome.PASS


def test_a_failed_thesis_and_a_breached_cap_each_block():
    signals = [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.EXPANSION)]
    (broken,) = raise_on(ISIN, D0, signals, state(thesis_intact=False))
    assert broken.blocked_by == (Gate.THESIS_INTACT,)
    (over,) = raise_on(ISIN, D0, signals, state(weight_pct=Decimal("9")))
    assert over.blocked_by == (Gate.CONCENTRATION_WITHIN_CAP,)


@pytest.mark.parametrize(
    "missing,expected",
    [
        ({"close": None}, Gate.PRICE_BELOW_BASE_OR_BULL),
        ({"base_value": None}, Gate.PRICE_BELOW_BASE_OR_BULL),
        ({"thesis_intact": None}, Gate.THESIS_INTACT),
        ({"weight_pct": None}, Gate.CONCENTRATION_WITHIN_CAP),
        ({"cap_pct": None}, Gate.CONCENTRATION_WITHIN_CAP),
    ],
)
def test_a_gate_whose_inputs_were_not_known_is_unknown_and_blocks(missing, expected):
    signals = [sig(SignalCategory.OWNERSHIP), sig(SignalCategory.EXPANSION)]
    (r,) = raise_on(ISIN, D0, signals, state(**missing))
    assert gate(r, expected) is GateOutcome.UNKNOWN
    assert not r.would_have_raised


def test_trim_and_exit_reviews_are_not_gated():
    for action in (ReviewAction.TRIM_REVIEW, ReviewAction.EXIT_REVIEW):
        (r,) = raise_on(ISIN, D0, [sig(SignalCategory.THESIS, action)], state(thesis_intact=False))
        assert r.gates == ()
        assert r.would_have_raised


# --- R2: the raising side sees nothing dated after the raise -----------------


def test_a_signal_observed_after_the_raise_date_is_look_ahead():
    with pytest.raises(LookAhead, match="observed after"):
        raise_on(ISIN, D0, [sig(SignalCategory.OWNERSHIP, on=day(1))], state())


def test_a_state_known_after_the_raise_date_is_look_ahead():
    with pytest.raises(LookAhead, match="is after the raise date"):
        raise_on(ISIN, D0, [sig(SignalCategory.OWNERSHIP)], state(as_of=day(1)))


def test_a_raised_review_carries_no_outcome_field():
    # Structural, not cosmetic: an outcome reachable from Raised could reach a
    # prompt that makes a forward-looking judgement (R2).
    assert not {f for f in Raised.__dataclass_fields__} & set(Outcome.__dataclass_fields__) - {
        "isin",
        "raised_on",
        "action",
        "rule_version",
    }


def test_replay_groups_by_isin_and_date_and_is_ordered_oldest_first():
    other = "INE009A01021"
    signals = [
        sig(SignalCategory.OWNERSHIP, on=day(5)),
        sig(SignalCategory.EXPANSION, on=day(5)),
        sig(SignalCategory.OWNERSHIP, on=D0, isin=other),
    ]
    states = {(ISIN, day(5)): state(as_of=day(5)), (other, D0): state()}
    out = replay(signals, states)
    assert [(r.isin, r.raised_on) for r in out] == [(other, D0), (ISIN, day(5))]
    assert out[1].would_have_raised


def test_a_missing_state_is_replayed_as_unknown_not_dropped():
    (r,) = replay([sig(SignalCategory.OWNERSHIP), sig(SignalCategory.EXPANSION)], {})
    assert not r.would_have_raised
    assert set(r.blocked_by) == {
        Gate.PRICE_BELOW_BASE_OR_BULL,
        Gate.THESIS_INTACT,
        Gate.CONCENTRATION_WITHIN_CAP,
    }


# --- measuring --------------------------------------------------------------


def closes(**by_offset: str) -> dict[date, Decimal]:
    return {day(int(k[1:])): Decimal(v) for k, v in by_offset.items()}


def raised(action: ReviewAction = ReviewAction.ADD_REVIEW, on: date = D0) -> Raised:
    return Raised(ISIN, on, action, (SignalCategory.OWNERSHIP,), (), ())


def test_forward_return_is_measured_on_the_first_trading_day_at_or_after_the_horizon():
    series = closes(d0="100", d30="110", d33="121")
    o = measure(raised(), series, 30, measured_at=day(60))
    assert (o.observed_on, o.return_pct) == (day(30), Decimal("10.00"))
    o = measure(raised(), closes(d0="100", d33="121"), 30, measured_at=day(60))
    assert (o.observed_on, o.return_pct) == (day(33), Decimal("21.00"))


def test_excess_return_is_the_move_over_the_benchmark():
    o = measure(
        raised(),
        closes(d0="100", d30="110"),
        30,
        measured_at=day(60),
        benchmark=closes(d0="200", d30="208"),
    )
    assert (o.benchmark_return_pct, o.excess_return_pct) == (Decimal("4.00"), Decimal("6.00"))


def test_an_outcome_whose_horizon_has_not_passed_cannot_be_read():
    with pytest.raises(NotYetObservable, match="after the measurement date"):
        measure(raised(), closes(d0="100", d30="110"), 30, measured_at=day(29))


def test_a_horizon_beyond_the_end_of_the_series_is_not_observable():
    with pytest.raises(NotYetObservable, match="no close on or after"):
        measure(raised(), closes(d0="100"), 30, measured_at=day(60))


def test_a_non_positive_horizon_is_rejected():
    with pytest.raises(ValueError, match="must be positive"):
        measure(raised(), closes(d0="100", d30="110"), 0, measured_at=day(60))


# --- hit rates --------------------------------------------------------------


def outcome(
    action: ReviewAction,
    on: date,
    ret: str,
    horizon: int = 30,
    bench: str | None = None,
    raised: bool = True,
) -> Outcome:
    r = Decimal(ret)
    b = None if bench is None else Decimal(bench)
    return Outcome(
        isin=ISIN,
        raised_on=on,
        action=action,
        horizon_days=horizon,
        would_have_raised=raised,
        observed_on=on + timedelta(days=horizon),
        close_at_raise=Decimal("100"),
        close_at_horizon=Decimal("100") * (Decimal(1) + r / 100),
        return_pct=r,
        benchmark_return_pct=b,
        excess_return_pct=None if b is None else (r - b),
    )


TUNING = Window(date(2020, 1, 1), date(2022, 1, 1))
EVAL = Window(date(2022, 1, 1), date(2024, 1, 1))


def test_hit_counts_a_move_in_the_direction_the_review_implied():
    outcomes = [
        outcome(ReviewAction.ADD_REVIEW, day(0), "12"),
        outcome(ReviewAction.ADD_REVIEW, day(1), "-3"),
        outcome(ReviewAction.EXIT_REVIEW, day(2), "-20"),
        outcome(ReviewAction.EXIT_REVIEW, day(3), "5"),
    ]
    rates = {h.action: h for h in hit_rates(outcomes, EVAL, TUNING)}
    assert (rates[ReviewAction.ADD_REVIEW].hits, rates[ReviewAction.ADD_REVIEW].raised) == (1, 2)
    assert (rates[ReviewAction.EXIT_REVIEW].hits, rates[ReviewAction.EXIT_REVIEW].raised) == (1, 2)
    assert rates[ReviewAction.ADD_REVIEW].rate == Decimal("0.5000")


def test_only_raises_inside_the_evaluation_window_are_scored():
    outcomes = [
        outcome(ReviewAction.ADD_REVIEW, date(2021, 6, 1), "12"),
        outcome(ReviewAction.ADD_REVIEW, date(2022, 6, 1), "12"),
        outcome(ReviewAction.ADD_REVIEW, date(2024, 6, 1), "12"),
    ]
    (h,) = hit_rates(outcomes, EVAL, TUNING)
    assert h.raised == 1


def test_reporting_on_the_window_the_parameters_were_tuned_on_is_refused():
    with pytest.raises(OverlappingWindows):
        hit_rates([], EVAL, Window(date(2021, 1, 1), date(2022, 6, 1)))


def test_an_excess_hit_rate_is_refused_when_any_outcome_has_no_benchmark():
    outcomes = [
        outcome(ReviewAction.ADD_REVIEW, day(0), "12", bench="4"),
        outcome(ReviewAction.ADD_REVIEW, day(1), "12"),
    ]
    with pytest.raises(ValueError, match="no benchmark"):
        hit_rates(outcomes, EVAL, TUNING, basis=ReturnBasis.EXCESS)
    (h,) = hit_rates(outcomes[:1], EVAL, TUNING, basis=ReturnBasis.EXCESS)
    assert (h.hits, h.raised) == (1, 1)


def test_blocked_reviews_are_scored_separately_so_the_cost_of_a_gate_is_visible():
    outcomes = [
        outcome(ReviewAction.ADD_REVIEW, day(0), "12"),
        outcome(ReviewAction.ADD_REVIEW, day(1), "8"),
        outcome(ReviewAction.ADD_REVIEW, day(2), "-6", raised=False),
        outcome(ReviewAction.ADD_REVIEW, day(3), "-9", raised=False),
    ]
    rates = {h.would_have_raised: h for h in hit_rates(outcomes, EVAL, TUNING)}
    assert (rates[True].hits, rates[True].raised) == (2, 2)
    assert (rates[False].hits, rates[False].raised) == (0, 2)


def test_an_outcome_inherits_whether_its_review_was_actually_raised():
    blocked = Raised(
        ISIN, D0, ReviewAction.ADD_REVIEW, (SignalCategory.OWNERSHIP,),
        (GateResult(Gate.TWO_INDEPENDENT_CATEGORIES, GateOutcome.BLOCK, "1 category"),), (),
    )
    series = closes(d0="100", d30="110")
    assert measure(blocked, series, 30, measured_at=day(60)).would_have_raised is False
    assert measure(raised(), series, 30, measured_at=day(60)).would_have_raised is True


def test_horizons_are_reported_separately():
    outcomes = [
        outcome(ReviewAction.ADD_REVIEW, day(0), "12", horizon=30),
        outcome(ReviewAction.ADD_REVIEW, day(0), "-4", horizon=365),
    ]
    assert [(h.horizon_days, h.hits) for h in hit_rates(outcomes, EVAL, TUNING)] == [(30, 1), (365, 0)]


def test_no_outcomes_yields_no_rows_rather_than_a_flattering_zero():
    assert hit_rates([], EVAL, TUNING) == []


# --- properties -------------------------------------------------------------

money = st.decimals(min_value=Decimal("1"), max_value=Decimal("100000"), places=2)


@given(start=money, end=money)
def test_the_sign_of_a_return_always_matches_the_direction_of_the_move(start, end):
    series = {D0: start, day(30): end}
    o = measure(raised(), series, 30, measured_at=day(60))
    if end > start:
        assert o.return_pct >= 0
    elif end < start:
        assert o.return_pct <= 0


@given(
    start=money,
    end=money,
    action=st.sampled_from(list(ReviewAction)),
)
def test_add_and_exit_reviews_never_both_hit_on_the_same_move(start, end, action):
    o = measure(raised(action), {D0: start, day(30): end}, 30, measured_at=day(60))
    (h,) = hit_rates([o], Window(D0, day(1)), Window(date(2000, 1, 1), date(2001, 1, 1)))
    flipped = Outcome(**{**o.__dict__, "action": ReviewAction.EXIT_REVIEW
                         if action is ReviewAction.ADD_REVIEW else ReviewAction.ADD_REVIEW})
    (h2,) = hit_rates([flipped], Window(D0, day(1)), Window(date(2000, 1, 1), date(2001, 1, 1)))
    if o.return_pct != DEFAULT_HIT_THRESHOLD_PCT:
        assert h.hits + h2.hits == 1
