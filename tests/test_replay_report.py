from datetime import date, timedelta
from decimal import Decimal

import pytest

from core.compute.disclaimer import TEMPLATE, carries_disclaimer, disclaimer
from core.compute.replay import (
    Gate,
    GateOutcome,
    GateResult,
    HitRate,
    Outcome,
    Raised,
    ReturnBasis,
    ReviewAction,
    SignalCategory,
    Window,
)
from core.compute.replay_report import render, render_isin_index

ISIN = "INE848E01016"
D0 = date(2022, 1, 3)
AS_OF = date(2024, 1, 1)


def raised(action=ReviewAction.ADD_REVIEW, blocked=False, isin=ISIN, on=D0) -> Raised:
    gates = (
        GateResult(Gate.TWO_INDEPENDENT_CATEGORIES, GateOutcome.PASS, "2 independent categories"),
        GateResult(
            Gate.PRICE_BELOW_BASE_OR_BULL,
            GateOutcome.BLOCK if blocked else GateOutcome.PASS,
            "close 130 >= 120" if blocked else "close 100 < 120",
        ),
        GateResult(Gate.THESIS_INTACT, GateOutcome.PASS, "thesis intact"),
        GateResult(Gate.CONCENTRATION_WITHIN_CAP, GateOutcome.PASS, "weight 4% <= cap 8%"),
    )
    return Raised(
        isin,
        on,
        action,
        (SignalCategory.CREDIT_RATING, SignalCategory.OWNERSHIP),
        gates,
        ("https://example.test/evidence",),
    )


def outcome(ret="12.00", bench=None, horizon=365) -> Outcome:
    r = Decimal(ret)
    b = None if bench is None else Decimal(bench)
    return Outcome(
        isin=ISIN,
        raised_on=D0,
        action=ReviewAction.ADD_REVIEW,
        horizon_days=horizon,
        would_have_raised=True,
        observed_on=D0 + timedelta(days=horizon),
        close_at_raise=Decimal("100.00"),
        close_at_horizon=Decimal("112.00"),
        return_pct=r,
        benchmark_return_pct=b,
        excess_return_pct=None if b is None else r - b,
    )


# --- the disclaimer is not optional -----------------------------------------


def test_every_rendered_report_carries_the_disclaimer_for_its_own_as_of_date():
    for args in ([], [raised()], [raised(blocked=True)]):
        text = render(args, as_of=AS_OF)
        assert carries_disclaimer(text, AS_OF)


def test_a_report_does_not_satisfy_the_check_with_another_dates_disclaimer():
    text = render([raised()], as_of=AS_OF)
    assert not carries_disclaimer(text, date(2023, 1, 1))


def test_the_disclaimer_wording_is_the_one_claude_md_fixes():
    text = disclaimer(AS_OF)
    assert "**Assumed baseline.**" in text
    assert "not investment advice or an instruction to trade" in " ".join(text.split())
    assert "data known as of 2024-01-01" in " ".join(text.split())
    assert "{as_of}" in TEMPLATE  # the date is never hardcoded away


def test_the_check_tolerates_rewrapping_but_not_paraphrase():
    assert carries_disclaimer(" ".join(disclaimer(AS_OF).split()), AS_OF)
    assert not carries_disclaimer(disclaimer(AS_OF).replace("not investment advice", "guidance"), AS_OF)


# --- the report says review, never buy --------------------------------------


@pytest.mark.parametrize("action", list(ReviewAction))
def test_no_rendered_report_contains_a_buy_sell_or_hold_verdict(action):
    text = render(
        [raised(action), raised(action, blocked=True)],
        as_of=AS_OF,
        outcomes=[outcome()],
        rates=[
            HitRate(action, 365, True, ReturnBasis.ABSOLUTE, Decimal("0"), 10, 6, Decimal("0.6000"))
        ],
    )
    words = set(text.upper().replace("/", " ").split())
    assert not words & {"BUY", "SELL", "HOLD"}
    assert action.value in text


def test_a_blocked_review_is_shown_with_the_gate_that_blocked_it():
    text = render([raised(blocked=True)], as_of=AS_OF)
    assert "blocked" in text
    assert "gate price_below_base_or_bull: BLOCK" in text


def test_outcomes_are_labelled_as_outcome_data_and_attached_to_their_raise():
    text = render([raised()], as_of=AS_OF, outcomes=[outcome(bench="4.00")])
    assert "outcome @365d (2023-01-03): 100.00 -> 112.00, +12.00% (+8.00% vs benchmark)" in text
    assert "never an input to a forward-looking judgement" in text


def test_the_tuning_and_evaluation_windows_are_stated_when_hit_rates_are_shown():
    text = render(
        [raised()],
        as_of=AS_OF,
        rates=[
            HitRate(ReviewAction.ADD_REVIEW, 365, True, ReturnBasis.EXCESS, Decimal("0"), 10, 6, Decimal("0.6000")),
            HitRate(ReviewAction.ADD_REVIEW, 365, False, ReturnBasis.EXCESS, Decimal("0"), 4, 1, Decimal("0.2500")),
        ],
        tuning=Window(date(2018, 1, 1), date(2021, 1, 1)),
        evaluation=Window(date(2021, 1, 1), date(2024, 1, 1)),
    )
    assert "tuned on [2018-01-01 .. 2021-01-01)" in text
    assert "reported on [2021-01-01 .. 2024-01-01)" in text
    assert "ADD_REVIEW @365d (raised), excess return vs 0%: 6/10 = 60.0%" in text
    assert "ADD_REVIEW @365d (blocked by a gate), excess return vs 0%: 1/4 = 25.0%" in text


def test_an_empty_replay_says_so_rather_than_rendering_nothing():
    text = render([], as_of=AS_OF)
    assert "No signals in the replay window." in text


def test_reviews_are_ordered_oldest_first():
    other = "INE009A01021"
    text = render([raised(on=date(2023, 5, 1)), raised(isin=other, on=D0)], as_of=AS_OF)
    assert text.index(other) < text.index(f"2023-05-01  {ISIN}")


def test_the_isin_index_counts_only_reviews_that_would_have_been_raised():
    other = "INE009A01021"
    counts = render_isin_index(
        [raised(), raised(on=date(2023, 5, 1)), raised(blocked=True), raised(isin=other)]
    )
    assert counts == {other: 1, ISIN: 2}
