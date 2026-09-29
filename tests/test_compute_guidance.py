"""Everything about a guidance claim that code decides rather than a model.

The lexicon and the comparison here are the credibility ledger's grading
scale. A silent change in either would regrade every company at once, so the
tests below pin the distinctions the scale exists to make: that "we will" and
"we are working towards" are not the same promise, that an unknown phrase is
refused rather than bucketed, and that a number is read as printed and never
rescaled.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.guidance import (
    HEDGE_LEXICON,
    HEDGE_RANK,
    ComparableClaim,
    Direction,
    HedgeChange,
    HedgeStrength,
    ParsedValue,
    Specificity,
    Unit,
    ValueUnparsed,
    compare_claims,
    compare_hedges,
    hedge_strength,
    normalise_hedge,
    parse_value,
)

# --------------------------------------------------------------------------- #
# The lexicon
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("we will", HedgeStrength.WILL),
        ("We will definitely", HedgeStrength.WILL),
        ("the company is committed to", HedgeStrength.WILL),
        ("we are guiding for", HedgeStrength.WILL),
        ("we expect", HedgeStrength.EXPECT),
        ("management believes that", HedgeStrength.EXPECT),
        ("should", HedgeStrength.EXPECT),
        ("we are targeting", HedgeStrength.AIM_TO),
        ("we are looking at", HedgeStrength.AIM_TO),
        ("we are working towards", HedgeStrength.WORKING_TOWARDS),
        ("we are hoping for", HedgeStrength.WORKING_TOWARDS),
        ("we would like to", HedgeStrength.WORKING_TOWARDS),
    ],
)
def test_the_distinctions_the_ledger_turns_on(phrase, expected):
    assert hedge_strength(phrase) is expected


def test_will_and_working_towards_are_not_the_same_promise():
    """The whole reason strength is code and not a model output."""
    assert HEDGE_RANK[hedge_strength("we will")] > HEDGE_RANK[hedge_strength("we are working towards")]


@pytest.mark.parametrize(
    "phrase",
    ["we might conceivably", "it is not impossible that", "", "   ", "we shan't rule out"],
)
def test_an_unknown_phrase_is_refused_not_bucketed(phrase):
    """None means quarantine. A default would forgive or invent a commitment."""
    assert hedge_strength(phrase) is None


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("  WE   ARE   WORKING  TOWARDS ", "working"),
        ("The company is working towards", "working"),
        ("They have committed to", "committed"),
        ("So we would expect", "expect"),
        ("I am targeting", "targeting"),
    ],
)
def test_normalise_strips_the_sentence_and_leaves_the_verb(phrase, expected):
    assert normalise_hedge(phrase) == expected


def test_every_lexicon_key_is_already_normalised():
    """A key that normalises to something else could never be looked up."""
    assert {k for k in HEDGE_LEXICON if normalise_hedge(k) != k} == set()


def test_every_strength_is_ranked():
    assert set(HEDGE_RANK) == set(HedgeStrength)
    assert len(set(HEDGE_RANK.values())) == len(HedgeStrength)


@given(st.sampled_from(sorted(HEDGE_LEXICON)))
def test_a_known_phrase_survives_being_dressed_up_in_a_sentence(key):
    assert hedge_strength(f"  We are {key.upper()}  ") in HedgeStrength


# --------------------------------------------------------------------------- #
# compare_hedges
# --------------------------------------------------------------------------- #


def test_hedge_change_reads_the_wording_not_the_number():
    assert compare_hedges(HedgeStrength.AIM_TO, HedgeStrength.WILL) is HedgeChange.STRENGTHENED
    assert compare_hedges(HedgeStrength.WILL, HedgeStrength.WORKING_TOWARDS) is HedgeChange.WEAKENED
    assert compare_hedges(HedgeStrength.EXPECT, HedgeStrength.EXPECT) is HedgeChange.UNCHANGED


@given(st.sampled_from(list(HedgeStrength)), st.sampled_from(list(HedgeStrength)))
def test_hedge_change_is_antisymmetric(a, b):
    opposite = {
        HedgeChange.STRENGTHENED: HedgeChange.WEAKENED,
        HedgeChange.WEAKENED: HedgeChange.STRENGTHENED,
        HedgeChange.UNCHANGED: HedgeChange.UNCHANGED,
    }
    assert compare_hedges(b, a) is opposite[compare_hedges(a, b)]


# --------------------------------------------------------------------------- #
# parse_value
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "low", "high", "unit", "specificity"),
    [
        ("15%", "15", "15", Unit.PERCENT, Specificity.POINT),
        ("15.5 per cent", "15.5", "15.5", Unit.PERCENT, Specificity.POINT),
        ("15-17%", "15", "17", Unit.PERCENT, Specificity.RANGE),
        ("17 to 15 percent", "15", "17", Unit.PERCENT, Specificity.RANGE),
        ("at least 15%", "15", None, Unit.PERCENT, Specificity.BOUND),
        ("north of 15%", "15", None, Unit.PERCENT, Specificity.BOUND),
        ("below 15%", None, "15", Unit.PERCENT, Specificity.BOUND),
        ("up to 15%", None, "15", Unit.PERCENT, Specificity.BOUND),
        ("200 bps", "200", "200", Unit.BPS, Specificity.POINT),
        ("200 basis points", "200", "200", Unit.BPS, Specificity.POINT),
        ("Rs 2,000 crore", "2000", "2000", Unit.INR_CRORE, Specificity.POINT),
        ("₹2,000 crore", "2000", "2000", Unit.INR_CRORE, Specificity.POINT),
        ("1.5x", "1.5", "1.5", Unit.MULTIPLE, Specificity.POINT),
        ("4 times", "4", "4", Unit.MULTIPLE, Specificity.POINT),
        ("35 stores", "35", "35", Unit.COUNT, Specificity.POINT),
    ],
)
def test_the_number_as_printed(text, low, high, unit, specificity):
    value = parse_value(text)
    assert value.low == (Decimal(low) if low else None)
    assert value.high == (Decimal(high) if high else None)
    assert value.unit is unit
    assert value.specificity is specificity


@pytest.mark.parametrize("text", [None, "", "   "])
def test_no_number_is_directional_not_an_error(text):
    value = parse_value(text)
    assert value.specificity is Specificity.DIRECTIONAL
    assert value.low is None and value.high is None


def test_the_quote_supplies_the_unit_when_the_phrase_does_not():
    assert parse_value("15", unit_hint="EBITDA margin of 15% in FY27").unit is Unit.PERCENT
    assert parse_value("15").unit is Unit.COUNT


def test_money_is_decimal_never_float():
    value = parse_value("Rs 2,000.55 crore")
    assert isinstance(value.low, Decimal)
    assert value.low == Decimal("2000.55")


def test_a_lakh_figure_is_read_as_printed_not_rescaled_into_crore():
    """Rescaling here is exactly the arithmetic-on-a-guess R1 keeps out."""
    value = parse_value("50,000 lakh")
    assert value.low == Decimal("50000")
    assert value.unit is Unit.COUNT


@pytest.mark.parametrize("text", ["good growth", "15-17-19%", "several hundred crore"])
def test_a_phrase_the_parser_cannot_read_raises_rather_than_guessing(text):
    with pytest.raises(ValueUnparsed):
        parse_value(text)


@given(st.decimals(min_value=0, max_value=10_000, places=2), st.decimals(min_value=0, max_value=10_000, places=2))
def test_a_two_number_phrase_always_comes_back_low_then_high(a, b):
    value = parse_value(f"{a} to {b} percent")
    assert value.low <= value.high
    assert {value.low, value.high} == {a, b}


# --------------------------------------------------------------------------- #
# compare_claims
# --------------------------------------------------------------------------- #


def claim(low, high, *, metric="ebitda_margin", period="FY27", unit=Unit.PERCENT, spec=None, hedge=HedgeStrength.EXPECT):
    if spec is None:
        spec = Specificity.POINT if low == high else Specificity.RANGE
    return ComparableClaim(
        metric=metric,
        period_label=period,
        value=ParsedValue(
            Decimal(low) if low is not None else None,
            Decimal(high) if high is not None else None,
            unit,
            spec,
        ),
        hedge=hedge,
    )


def test_the_first_claim_for_a_metric_is_new():
    assert compare_claims(None, claim("15", "15")) is Direction.NEW


@pytest.mark.parametrize(
    ("prev", "curr", "expected"),
    [
        (("15", "15"), ("16", "16"), Direction.RAISED),
        (("15", "15"), ("14", "14"), Direction.LOWERED),
        (("15", "15"), ("15", "15"), Direction.MAINTAINED),
        (("15", "17"), ("16", "18"), Direction.RAISED),
        (("15", "17"), ("14", "18"), Direction.RESHAPED),
        (("15", "17"), ("16", "16"), Direction.RESHAPED),  # floor up, ceiling down
    ],
)
def test_direction_against_the_prior_claim(prev, curr, expected):
    assert compare_claims(claim(*prev), claim(*curr)) is expected


def test_a_floor_is_compared_on_the_side_it_states():
    floor = lambda v: claim(v, None, spec=Specificity.BOUND)  # noqa: E731
    assert compare_claims(floor("15"), floor("16")) is Direction.RAISED
    assert compare_claims(floor("15"), floor("15")) is Direction.MAINTAINED


def test_a_floor_and_a_ceiling_line_up_on_nothing():
    floor = claim("15", None, spec=Specificity.BOUND)
    ceiling = claim(None, "15", spec=Specificity.BOUND)
    assert compare_claims(floor, ceiling) is Direction.NOT_COMPARABLE


@pytest.mark.parametrize(
    "current",
    [
        claim("16", "16", metric="revenue_growth"),
        claim("16", "16", period="FY28"),
        claim("16", "16", unit=Unit.BPS),
        claim(None, None, spec=Specificity.DIRECTIONAL),
    ],
)
def test_claims_that_do_not_line_up_are_never_compared_on_a_guess(current):
    assert compare_claims(claim("15", "15"), current) is Direction.NOT_COMPARABLE


def test_a_directional_prior_is_not_comparable_either():
    directional = claim(None, None, spec=Specificity.DIRECTIONAL)
    assert compare_claims(directional, claim("15", "15")) is Direction.NOT_COMPARABLE


def test_withdrawn_is_not_produced_here():
    """The absence of a claim is SILENT, resolved elsewhere against filings."""
    assert "withdrawn" not in {d.value for d in Direction}


@given(
    st.decimals(min_value=1, max_value=100, places=1),
    st.decimals(min_value=1, max_value=100, places=1),
)
def test_raised_and_lowered_are_mirror_images(a, b):
    first, second = claim(str(a), str(a)), claim(str(b), str(b))
    mirror = {
        Direction.RAISED: Direction.LOWERED,
        Direction.LOWERED: Direction.RAISED,
        Direction.MAINTAINED: Direction.MAINTAINED,
    }
    assert compare_claims(second, first) is mirror[compare_claims(first, second)]


def test_the_hedge_does_not_move_the_direction():
    """Wording and number are graded separately, and reported separately."""
    weak = claim("15", "15", hedge=HedgeStrength.WORKING_TOWARDS)
    strong = claim("15", "15", hedge=HedgeStrength.WILL)
    assert compare_claims(weak, strong) is Direction.MAINTAINED
    assert compare_hedges(weak.hedge, strong.hedge) is HedgeChange.STRENGTHENED
