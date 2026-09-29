"""Grading a guidance claim against reported facts, and telling silence from delivery.

The resolution rule decides which claims management is credited with keeping.
These tests pin the edges of that decision: that a range is inclusive, that a
floor is exact, that 5% is inside a point target and 5.0001% is not, that a
number is never rescaled into a unit the claim did not use, and that a claim
whose answer nobody can compute is said to be unresolvable rather than
quietly counted as kept or broken.

Every metric test states its arithmetic in a comment, worked by hand.
"""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute import ratios
from core.compute.guidance import (
    HEDGE_RANK,
    ComparableClaim,
    HedgeStrength,
    ParsedValue,
    Specificity,
    Unit,
)
from core.compute.guidance_resolution import (
    DEFAULT_RULE,
    METRIC_RESOLVERS,
    RULE_VERSION,
    Basis,
    ClaimOutcome,
    Fact,
    FiscalPeriod,
    PeriodKind,
    ResolutionRule,
    Status,
    TranscriptMentions,
    UnresolvableReason,
    claim_key,
    delivery_rate,
    detect_silent,
    grade,
    parse_period_label,
    period_key,
    period_resolved,
    resolve_claim,
    select_claims,
)

D = Decimal
CRORE = D(10**7)
C, S = Basis.CONSOLIDATED, Basis.STANDALONE

FY27 = (date(2026, 4, 1), date(2027, 3, 31))
FY26 = (date(2025, 4, 1), date(2026, 3, 31))
Q3_FY26 = (date(2025, 10, 1), date(2025, 12, 31))
Q3_FY25 = (date(2024, 10, 1), date(2024, 12, 31))


def crore(n) -> Decimal:
    """`n` crore as the absolute rupees a financial fact carries."""
    return D(n) * CRORE


def duration(item, span, value, basis=C) -> Fact:
    return Fact(item, span[0], span[1], D(value), basis)


def instant(item, on, value, basis=C) -> Fact:
    return Fact(item, None, on, D(value), basis)


def claim(metric, label, low, high=..., unit=Unit.INR_CRORE, hedge=HedgeStrength.WILL):
    """A ComparableClaim; `high` defaults to `low` (a point)."""
    high = low if high is ... else high
    low, high = (None if x is None else D(x) for x in (low, high))
    if low is None and high is None:
        spec = Specificity.DIRECTIONAL
    elif low is None or high is None:
        spec = Specificity.BOUND
    elif low == high:
        spec = Specificity.POINT
    else:
        spec = Specificity.RANGE
    return ComparableClaim(metric, label, ParsedValue(low, high, unit, spec), hedge)


# --------------------------------------------------------------------------- #
# Period labels
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "kind", "start", "end"),
    [
        # FY27 is 1 Apr 2026 - 31 Mar 2027, however it is spelled
        ("FY27", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("FY2027", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("FY 2026-27", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("FY26-27", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("FY2026-2027", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("fiscal 2027", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("Fiscal Year 2027", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        ("  fy   27 ", PeriodKind.FY, date(2026, 4, 1), date(2027, 3, 31)),
        # quarters and halves
        ("Q1 FY27", PeriodKind.Q1, date(2026, 4, 1), date(2026, 6, 30)),
        ("Q2FY27", PeriodKind.Q2, date(2026, 7, 1), date(2026, 9, 30)),
        ("Q3 FY26", PeriodKind.Q3, date(2025, 10, 1), date(2025, 12, 31)),
        ("Q3FY26", PeriodKind.Q3, date(2025, 10, 1), date(2025, 12, 31)),
        ("q4 fy27", PeriodKind.Q4, date(2027, 1, 1), date(2027, 3, 31)),
        ("Q4 FY 2026-27", PeriodKind.Q4, date(2027, 1, 1), date(2027, 3, 31)),
        ("H1 FY27", PeriodKind.H1, date(2026, 4, 1), date(2026, 9, 30)),
        ("h2fy27", PeriodKind.H2, date(2026, 10, 1), date(2027, 3, 31)),
    ],
)
def test_every_accepted_period_label_form(label, kind, start, end):
    assert parse_period_label(label) == FiscalPeriod(kind, start, end)


@pytest.mark.parametrize(
    "label",
    [
        "next 3 years",
        "medium term",
        "by 2030",
        "this year",
        "",
        "   ",
        "FY",
        "Q3",  # no year: which Q3?
        "Q5 FY27",
        "H3 FY27",
        "FY2026-28",  # not consecutive years
        "FY 12345",
        "FY27 and FY28",
        "the next fiscal",
    ],
)
def test_a_label_that_names_no_fiscal_period_is_none(label):
    assert parse_period_label(label) is None


def test_the_fiscal_year_boundary_is_april():
    """Q4 of FY27 falls in calendar 2027; Q1 of FY27 in calendar 2026."""
    q4 = parse_period_label("Q4 FY27")
    q1 = parse_period_label("Q1 FY27")
    assert q4.period_end.year == 2027 and q1.period_start.year == 2026


def test_two_forms_of_one_period_share_a_key():
    assert period_key("FY27") == period_key("FY 2026-27") == (PeriodKind.FY, date(2027, 3, 31))
    assert period_key("Q4 FY27") != period_key("FY27")  # same end date, different period


def test_an_unparsed_label_keys_on_its_normalised_text():
    assert period_key("  Medium   TERM ") == "medium term"
    assert claim_key("revenue", "Medium Term") == ("revenue", "medium term")


@given(st.text())
def test_parsing_never_raises_on_arbitrary_text(text):
    result = parse_period_label(text)
    assert result is None or isinstance(result, FiscalPeriod)


@given(st.text(alphabet="FYQHfyqh 0123456789-/.,fiscalyear\n\t", max_size=24))
def test_parsing_never_raises_near_the_grammar(text):
    result = parse_period_label(text)
    if result is not None:
        assert result.period_start < result.period_end


@given(st.integers(min_value=2001, max_value=2099), st.sampled_from(list(PeriodKind)))
def test_every_parsed_period_is_whole_months_inside_its_fiscal_year(year, kind):
    label = f"FY{year}" if kind is PeriodKind.FY else f"{kind.value} FY{year % 100:02d}"
    period = parse_period_label(label)
    assert period is not None and period.kind is kind
    assert period.period_start.day == 1
    assert date(year - 1, 4, 1) <= period.period_start < period.period_end <= date(year, 3, 31)


# --------------------------------------------------------------------------- #
# grade: each branch, at its boundaries
# --------------------------------------------------------------------------- #


def graded(value: ParsedValue, actual, unit=Unit.PERCENT, rule=DEFAULT_RULE):
    return grade(value, D(actual), unit, rule)


def rng(low, high, unit=Unit.PERCENT):
    return ParsedValue(D(low), D(high), unit, Specificity.RANGE)


def floor(low, unit=Unit.PERCENT):
    return ParsedValue(D(low), None, unit, Specificity.BOUND)


def ceiling(high, unit=Unit.PERCENT):
    return ParsedValue(None, D(high), unit, Specificity.BOUND)


def point(v, unit=Unit.PERCENT):
    return ParsedValue(D(v), D(v), unit, Specificity.POINT)


@pytest.mark.parametrize(
    ("actual", "status"),
    [
        ("14.99", Status.MISSED),
        ("15", Status.MET),  # the low end is inside
        ("16", Status.MET),
        ("17", Status.MET),  # so is the high end
        ("17.01", Status.MISSED),
    ],
)
def test_a_range_is_inclusive_and_exact(actual, status):
    assert graded(rng(15, 17), actual) == (status, None)


@pytest.mark.parametrize(
    ("actual", "status"),
    [("14.999999", Status.MISSED), ("15", Status.MET), ("15.000001", Status.MET), ("40", Status.MET)],
)
def test_a_floor_is_exact_and_inclusive(actual, status):
    assert graded(floor(15), actual) == (status, None)


@pytest.mark.parametrize(
    ("actual", "status"),
    [("-3", Status.MET), ("15", Status.MET), ("15.000001", Status.MISSED)],
)
def test_a_ceiling_is_exact_and_inclusive(actual, status):
    assert graded(ceiling(15), actual) == (status, None)


@pytest.mark.parametrize(
    ("actual", "status"),
    [
        # 5% of 15 is 0.75, so the window is 14.25 to 15.75 inclusive
        ("15", Status.MET),
        ("15.75", Status.MET),  # exactly 5%
        ("14.25", Status.MET),
        ("15.7500001", Status.MISSED),  # just over
        ("14.2499999", Status.MISSED),
    ],
)
def test_a_point_is_met_within_five_percent_relative(actual, status):
    assert graded(point(15), actual) == (status, None)


def test_a_point_tolerance_is_relative_to_the_target_not_absolute():
    # 5% of 2000 crore is 100 crore
    two_thousand = point(2000, Unit.INR_CRORE)
    assert graded(two_thousand, "2100", Unit.INR_CRORE)[0] is Status.MET
    assert graded(two_thousand, "2100.01", Unit.INR_CRORE)[0] is Status.MISSED


def test_a_negative_point_is_graded_on_its_magnitude():
    # net debt of -100 crore (net cash): 5% of |-100| is 5, so -105 to -95
    assert graded(point(-100, Unit.INR_CRORE), "-95", Unit.INR_CRORE)[0] is Status.MET
    assert graded(point(-100, Unit.INR_CRORE), "-94.9", Unit.INR_CRORE)[0] is Status.MISSED


def test_a_zero_point_needs_an_exact_match():
    assert graded(point(0), "0") == (Status.MET, None)
    assert graded(point(0), "0.0000001")[0] is Status.MISSED
    assert graded(point(0), "-0.0000001")[0] is Status.MISSED


def test_the_tolerance_lives_in_the_rule_object_and_is_replaceable():
    assert DEFAULT_RULE.point_tolerance == D("0.05")
    strict = ResolutionRule(point_tolerance=D("0.01"))
    assert graded(point(15), "15.2", rule=strict)[0] is Status.MISSED  # 0.2 > 0.15
    assert graded(point(15), "15.2")[0] is Status.MET


def test_the_rule_is_frozen():
    with pytest.raises(AttributeError):
        DEFAULT_RULE.point_tolerance = D("0.5")  # type: ignore[misc]


@pytest.mark.parametrize("kwargs", [{"point_tolerance": D("-0.01")}, {"consecutive_calls": 0}])
def test_a_nonsense_rule_is_refused_when_built(kwargs):
    with pytest.raises(ValueError):
        ResolutionRule(**kwargs)


def test_a_directional_claim_is_ungradable_before_any_number_is_looked_at():
    directional = ParsedValue(None, None, Unit.COUNT, Specificity.DIRECTIONAL)
    assert graded(directional, 15) == (Status.UNRESOLVABLE, UnresolvableReason.DIRECTIONAL)


@pytest.mark.parametrize(
    "value",
    [
        ParsedValue(D(50), D(50), Unit.BPS, Specificity.POINT),  # a change, not a level
        ParsedValue(D(15), D(15), Unit.COUNT, Specificity.POINT),
        ParsedValue(D(15), D(15), Unit.INR_CRORE, Specificity.POINT),
        ParsedValue(D(15), None, Unit.MULTIPLE, Specificity.BOUND),
    ],
)
def test_a_claim_in_another_unit_is_not_converted(value):
    assert graded(value, 15, Unit.PERCENT) == (Status.UNRESOLVABLE, UnresolvableReason.UNIT_MISMATCH)


@pytest.mark.parametrize(
    "value",
    [
        ParsedValue(None, None, Unit.PERCENT, Specificity.POINT),
        ParsedValue(D(15), None, Unit.PERCENT, Specificity.RANGE),
        ParsedValue(D(15), D(17), Unit.PERCENT, Specificity.BOUND),
        ParsedValue(None, None, Unit.PERCENT, Specificity.BOUND),
    ],
)
def test_a_claim_whose_numbers_do_not_match_its_shape_is_ungradable(value):
    assert graded(value, 15) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)


# --------------------------------------------------------------------------- #
# Each metric against a hand-computed actual
# --------------------------------------------------------------------------- #


def resolved(metric, label, facts):
    """Resolve a claim in the metric's own unit; the tests read the actual back."""
    unit = METRIC_RESOLVERS[metric].unit
    return resolve_claim(claim(metric, label, 1, unit=unit), facts)


def test_revenue_is_reported_rupees_shown_in_crore():
    # 123,450,000,000 INR / 10,000,000 = 12,345 crore
    facts = [duration(ratios.REVENUE, FY27, 123_450_000_000)]
    res = resolved("revenue", "FY27", facts)
    assert res.actual == D(12345) and res.unit is Unit.INR_CRORE and res.basis is C


def test_revenue_growth_is_year_on_year_in_percent_points():
    # 1,150 crore against 1,000 crore a year earlier: 1150 / 1000 - 1 = 0.15 -> 15
    facts = [duration(ratios.REVENUE, FY27, crore(1150)), duration(ratios.REVENUE, FY26, crore(1000))]
    res = resolved("revenue_growth", "FY27", facts)
    assert res.actual == D(15) and res.unit is Unit.PERCENT


def test_revenue_growth_for_a_quarter_compares_the_same_quarter_a_year_earlier():
    # Q3 FY26 is 900 crore, Q3 FY25 was 1,000 crore: 900 / 1000 - 1 = -0.10 -> -10
    facts = [
        duration(ratios.REVENUE, Q3_FY26, crore(900)),
        duration(ratios.REVENUE, Q3_FY25, crore(1000)),
        duration(ratios.REVENUE, (date(2025, 7, 1), date(2025, 9, 30)), crore(5000)),  # not the comparison
    ]
    assert resolved("revenue_growth", "Q3 FY26", facts).actual == D(-10)


def test_revenue_growth_without_the_prior_year_is_missing_input_not_open():
    """This year's revenue is public; what is missing is the comparison."""
    res = resolved("revenue_growth", "FY27", [duration(ratios.REVENUE, FY27, crore(1150))])
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)


def test_revenue_growth_from_a_non_positive_base_is_undefined_not_zero():
    facts = [duration(ratios.REVENUE, FY27, crore(1150)), duration(ratios.REVENUE, FY26, 0)]
    res = resolved("revenue_growth", "FY27", facts)
    assert (res.status, res.reason, res.actual) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING, None)


def test_pat_is_profit_for_the_period_in_crore():
    # 2,500,000,000 INR / 10^7 = 250 crore
    assert resolved("pat", "FY27", [duration(ratios.PROFIT, FY27, 2_500_000_000)]).actual == D(250)


def test_pat_margin_is_profit_over_revenue_in_percent_points():
    # 250 crore / 1,000 crore = 0.25 -> 25
    facts = [duration(ratios.PROFIT, FY27, crore(250)), duration(ratios.REVENUE, FY27, crore(1000))]
    assert resolved("pat_margin", "FY27", facts).actual == D(25)


def test_ebitda_is_revenue_less_costs_before_interest_and_depreciation():
    # revenue 1000; expenses 800 of which finance costs 30, depreciation 50
    # 1000 - (800 - 30 - 50) = 1000 - 720 = 280 crore
    facts = [
        duration(ratios.REVENUE, FY27, crore(1000)),
        duration(ratios.TOTAL_EXPENSES, FY27, crore(800)),
        duration(ratios.FINANCE_COSTS, FY27, crore(30)),
        duration(ratios.DEPRECIATION, FY27, crore(50)),
    ]
    res = resolved("ebitda", "FY27", facts)
    assert res.actual == D(280) and res.unit is Unit.INR_CRORE
    assert set(res.inputs) == {(f.line_item, f.period_end, f.value) for f in facts}
    # 280 / 1000 = 0.28 -> 28
    assert resolved("ebitda_margin", "FY27", facts).actual == D(28)


def test_net_debt_is_the_balance_on_the_period_end_date():
    # borrowings 300 + 500 = 800; less cash 100, other bank balances 50,
    # current investments 150 -> 800 - 300 = 500 crore
    on = FY27[1]
    facts = [
        instant(ratios.BORROWINGS_CURRENT, on, crore(300)),
        instant(ratios.BORROWINGS_NONCURRENT, on, crore(500)),
        instant(ratios.CASH, on, crore(100)),
        instant(ratios.OTHER_BANK_BALANCES, on, crore(50)),
        instant(ratios.CURRENT_INVESTMENTS, on, crore(150)),
        instant(ratios.CASH, date(2026, 3, 31), crore(9999)),  # the opening balance is not the answer
    ]
    res = resolved("net_debt", "FY27", facts)
    assert res.actual == D(500) and res.unit is Unit.INR_CRORE


def test_net_debt_can_be_negative_net_cash():
    on = FY27[1]
    facts = [
        instant(ratios.BORROWINGS_CURRENT, on, crore(10)),
        instant(ratios.BORROWINGS_NONCURRENT, on, crore(20)),
        instant(ratios.CASH, on, crore(100)),
        instant(ratios.OTHER_BANK_BALANCES, on, 0),
        instant(ratios.CURRENT_INVESTMENTS, on, crore(50)),
    ]
    assert resolved("net_debt", "FY27", facts).actual == D(-120)  # 30 - 150


def test_capex_is_property_plant_and_equipment_purchases_only():
    """Intangibles, investment property and other long-term assets are not counted."""
    item = "purchase_of_property_plant_and_equipment_classified_as_investing_activities"
    facts = [
        duration(item, FY27, crore(1234)),
        duration("purchase_of_intangible_assets_classified_as_investing_activities", FY27, crore(500)),
    ]
    assert resolved("capex", "FY27", facts).actual == D(1234)


def test_roce_is_ebit_over_the_average_of_opening_and_closing_capital_employed():
    # EBIT = PBT 700 + finance costs 100 = 800 crore
    # closing capital = equity 3000 + borrowings (200 + 400) = 3600
    # opening capital = equity 2400 + borrowings (100 + 300) = 2800
    # average = 3200; 800 / 3200 = 0.25 -> 25
    end, opening = FY27[1], FY26[1]
    facts = [
        duration(ratios.PROFIT_BEFORE_TAX, FY27, crore(700)),
        duration(ratios.FINANCE_COSTS, FY27, crore(100)),
        instant(ratios.EQUITY, end, crore(3000)),
        instant(ratios.BORROWINGS_CURRENT, end, crore(200)),
        instant(ratios.BORROWINGS_NONCURRENT, end, crore(400)),
        instant(ratios.EQUITY, opening, crore(2400)),
        instant(ratios.BORROWINGS_CURRENT, opening, crore(100)),
        instant(ratios.BORROWINGS_NONCURRENT, opening, crore(300)),
    ]
    res = resolved("roce", "FY27", facts)
    assert res.actual == D(25) and res.unit is Unit.PERCENT
    without_opening = [f for f in facts if f.period_end != opening]
    res = resolved("roce", "FY27", without_opening)
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)


def test_roce_for_less_than_a_fiscal_year_is_not_supported():
    """ratios/1 defines ROCE for fiscal years only; a quarter's is not derived."""
    res = resolved("roce", "Q3 FY26", [duration(ratios.PROFIT_BEFORE_TAX, Q3_FY26, crore(700))])
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.PERIOD_NOT_SUPPORTED)


@pytest.mark.parametrize(
    "metric",
    [
        "gross_margin", "volume_growth", "order_inflow", "order_book", "roe", "capacity",
        "store_count", "employee_count", "tax_rate", "dividend_payout", "other", "never_heard_of_it", "",
    ],
)  # fmt: skip
def test_a_metric_xbrl_cannot_answer_is_a_data_gap_not_an_approximation(metric):
    res = resolve_claim(claim(metric, "FY27", 15, unit=Unit.PERCENT), [duration(ratios.REVENUE, FY27, crore(1))])
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.METRIC_NOT_IN_XBRL)
    assert res.actual is None and res.inputs == ()


def test_the_resolver_table_agrees_with_the_extractable_metrics():
    """A typo in a key would leave a metric permanently unresolvable without a word."""
    from extract.guidance import Metric

    known = {m.value for m in Metric}
    assert set(METRIC_RESOLVERS) <= known
    assert set(METRIC_RESOLVERS) == {
        "revenue", "revenue_growth", "pat", "pat_margin", "ebitda", "ebitda_margin", "net_debt", "capex", "roce",
    }  # fmt: skip


def test_every_line_item_a_resolver_reads_is_a_real_xbrl_line_item():
    from ingest.nse_xbrl.mapping import LINE_ITEMS

    mapped = {name for table in LINE_ITEMS.values() for name, _unit in table.values()}
    needed = {n.line_item for r in METRIC_RESOLVERS.values() for n in r.needs}
    assert needed <= mapped, sorted(needed - mapped)


def test_the_basis_values_are_the_stores_consolidation_values():
    from core.db.models import Consolidation

    assert {b.value for b in Basis} == {c.value for c in Consolidation}


# --------------------------------------------------------------------------- #
# resolve_claim: grading, OPEN, basis, already known
# --------------------------------------------------------------------------- #


def test_a_claim_is_graded_against_the_actual_and_every_number_traces_to_a_fact():
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    res = resolve_claim(claim("revenue", "FY27", 950, 1050), facts)
    assert res.status is Status.MET and res.reason is None
    assert res.actual == D(1000)
    assert res.inputs == ((ratios.REVENUE, FY27[1], crore(1000)),)
    assert res.period == parse_period_label("FY27")
    assert res.rule_version == RULE_VERSION == "guidance-resolution-2026-09-29"


def test_a_missed_claim_says_so():
    facts = [duration(ratios.REVENUE, FY27, crore(900))]
    assert resolve_claim(claim("revenue", "FY27", 950, 1050), facts).status is Status.MISSED


def test_open_when_the_period_facts_are_not_yet_reported():
    """FY27 revenue is not in the facts known at t: the answer is not in yet."""
    facts = [duration(ratios.REVENUE, FY26, crore(1000))]
    res = resolve_claim(claim("revenue", "FY27", 1100), facts)
    assert (res.status, res.reason, res.actual, res.inputs) == (Status.OPEN, None, None, ())
    assert res.basis is None
    assert resolve_claim(claim("revenue", "FY27", 1100), []).status is Status.OPEN


def test_open_turns_to_a_grade_once_the_facts_are_public():
    """The same claim, two reads: what was known at t1 and what is known at t2."""
    c = claim("revenue", "FY27", 1100)
    assert resolve_claim(c, []).status is Status.OPEN
    assert resolve_claim(c, [duration(ratios.REVENUE, FY27, crore(1100))]).status is Status.MET


def test_a_restated_fact_changes_the_grade():
    """facts_as_of hands over one version per period; a later one can flip the answer."""
    c = claim("revenue", "FY27", 950, 1050)
    assert resolve_claim(c, [duration(ratios.REVENUE, FY27, crore(1000))]).status is Status.MET
    assert resolve_claim(c, [duration(ratios.REVENUE, FY27, crore(1200))]).status is Status.MISSED


def test_consolidated_is_used_when_any_consolidated_fact_is_present_and_recorded():
    facts = [
        duration(ratios.REVENUE, FY27, crore(1000), C),
        duration(ratios.REVENUE, FY27, crore(400), S),
    ]
    res = resolve_claim(claim("revenue", "FY27", 1000), facts)
    assert res.basis is C and res.actual == D(1000)


def test_standalone_is_used_only_when_no_consolidated_fact_exists():
    res = resolve_claim(claim("revenue", "FY27", 400), [duration(ratios.REVENUE, FY27, crore(400), S)])
    assert res.basis is S and res.status is Status.MET


def test_bases_are_never_mixed_within_one_actual():
    """Consolidated revenue with only a standalone profit is a gap, not a blend."""
    facts = [duration(ratios.REVENUE, FY27, crore(1000), C), duration(ratios.PROFIT, FY27, crore(250), S)]
    res = resolve_claim(claim("pat_margin", "FY27", 25, unit=Unit.PERCENT), facts)
    assert res.basis is C
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)


def test_already_known_when_made_is_not_a_forecast():
    """The actual was public on the day of the claim: nothing to grade."""
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    res = resolve_claim(claim("revenue", "FY27", 1000), facts, facts_when_made=facts)
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.ALREADY_KNOWN_WHEN_MADE)
    assert res.actual == D(1000)  # kept so the finding can be checked; it is not a grade


def test_a_fact_that_arrived_after_the_claim_does_not_make_it_already_known():
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    res = resolve_claim(claim("revenue", "FY27", 1000), facts, facts_when_made=[])
    assert res.status is Status.MET


def test_a_fact_restated_after_the_claim_still_counts_as_known_then():
    """Known-when-made asks only whether an answer existed, not whether it was the same."""
    made = [duration(ratios.REVENUE, FY27, crore(1000))]
    now = [duration(ratios.REVENUE, FY27, crore(1200))]
    res = resolve_claim(claim("revenue", "FY27", 1000), now, facts_when_made=made)
    assert res.reason is UnresolvableReason.ALREADY_KNOWN_WHEN_MADE


def test_unresolvable_reasons_take_precedence_over_facts():
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    directional = claim("revenue", "FY27", None, None)
    assert resolve_claim(directional, facts).reason is UnresolvableReason.DIRECTIONAL
    assert resolve_claim(claim("revenue", "medium term", 1000), facts).reason is UnresolvableReason.PERIOD_UNPARSED
    mismatch = resolve_claim(claim("revenue", "FY27", 1000, unit=Unit.PERCENT), facts)
    assert mismatch.reason is UnresolvableReason.UNIT_MISMATCH
    assert mismatch.period is not None


def test_an_unparsed_period_is_recorded_as_none():
    res = resolve_claim(claim("revenue", "by 2030", 1000), [])
    assert res.period is None and res.status is Status.UNRESOLVABLE


def test_conflicting_duplicate_facts_are_a_caller_bug_not_a_guess():
    facts = [duration(ratios.REVENUE, FY27, crore(1000)), duration(ratios.REVENUE, FY27, crore(1100))]
    with pytest.raises(ValueError):
        resolve_claim(claim("revenue", "FY27", 1000), facts)


def test_a_repeated_identical_fact_is_harmless():
    fact = duration(ratios.REVENUE, FY27, crore(1000))
    assert resolve_claim(claim("revenue", "FY27", 1000), [fact, fact]).status is Status.MET


def test_a_negative_finance_cost_is_bad_input_not_a_crash():
    facts = [
        duration(ratios.REVENUE, FY27, crore(1000)),
        duration(ratios.TOTAL_EXPENSES, FY27, crore(800)),
        duration(ratios.FINANCE_COSTS, FY27, crore(-30)),
        duration(ratios.DEPRECIATION, FY27, crore(50)),
    ]
    res = resolve_claim(claim("ebitda", "FY27", 280), facts)
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)


# --------------------------------------------------------------------------- #
# delivery_rate
# --------------------------------------------------------------------------- #

W, E, A, T = (
    HedgeStrength.WILL,
    HedgeStrength.EXPECT,
    HedgeStrength.AIM_TO,
    HedgeStrength.WORKING_TOWARDS,
)


def test_delivery_rate_is_the_hedge_weighted_share_of_graded_claims_kept():
    # MET: WILL (4) + AIM_TO (2) = 6; MISSED: EXPECT (3) -> 6 / (6 + 3) = 2/3
    out = [
        ClaimOutcome(Status.MET, W),
        ClaimOutcome(Status.MET, A),
        ClaimOutcome(Status.MISSED, E),
    ]
    rate = delivery_rate(out)
    assert rate.rate == D(6) / D(9)
    assert (rate.met, rate.missed, rate.open, rate.unresolvable, rate.silent) == (2, 1, 0, 0, 0)


def test_a_strong_promise_missed_costs_more_than_a_weak_one():
    # kept WORKING_TOWARDS (1), missed WILL (4): 1 / 5; the other way round: 4 / 5
    strong_missed = delivery_rate([ClaimOutcome(Status.MET, T), ClaimOutcome(Status.MISSED, W)])
    weak_missed = delivery_rate([ClaimOutcome(Status.MET, W), ClaimOutcome(Status.MISSED, T)])
    assert strong_missed.rate == D(1) / D(5)
    assert weak_missed.rate == D(4) / D(5)


def test_only_met_and_missed_are_in_the_rate_and_the_rest_are_reported_apart():
    out = [
        ClaimOutcome(Status.MET, W),
        ClaimOutcome(Status.OPEN, W),
        ClaimOutcome(Status.OPEN, E),
        ClaimOutcome(Status.UNRESOLVABLE, W),
        ClaimOutcome(Status.OPEN, W, silent=True),
        ClaimOutcome(Status.UNRESOLVABLE, W, silent=True),
    ]
    rate = delivery_rate(out)
    assert rate.rate == D(1)  # nothing that is not graded counts against anyone
    assert (rate.met, rate.missed, rate.open, rate.unresolvable, rate.silent) == (1, 0, 2, 1, 2)


def test_nothing_graded_is_none_not_zero():
    assert delivery_rate([]).rate is None
    assert delivery_rate([ClaimOutcome(Status.OPEN, W), ClaimOutcome(Status.UNRESOLVABLE, W)]).rate is None


def test_all_missed_is_zero_and_all_met_is_one():
    assert delivery_rate([ClaimOutcome(Status.MISSED, W)]).rate == D(0)
    assert delivery_rate([ClaimOutcome(Status.MET, T)]).rate == D(1)


def test_the_rate_is_a_decimal():
    assert isinstance(delivery_rate([ClaimOutcome(Status.MET, W), ClaimOutcome(Status.MISSED, E)]).rate, Decimal)


def test_silence_on_a_graded_claim_is_a_contradiction_and_refused():
    with pytest.raises(ValueError):
        delivery_rate([ClaimOutcome(Status.MET, W, silent=True)])


def test_the_default_weights_are_the_ledgers_own_ranks():
    """delivery_rate reuses HEDGE_RANK; a second table would drift from it."""
    out = [ClaimOutcome(Status.MET, h) for h in HedgeStrength] + [ClaimOutcome(Status.MISSED, W)]
    total = sum(HEDGE_RANK.values())
    assert delivery_rate(out).rate == D(total) / D(total + HEDGE_RANK[W])


statuses = st.sampled_from(list(Status))
hedges = st.sampled_from(list(HedgeStrength))
outcomes = st.lists(st.builds(ClaimOutcome, statuses, hedges), max_size=30)


@given(outcomes)
def test_the_rate_is_between_zero_and_one_or_none(items):
    rate = delivery_rate(items).rate
    assert rate is None or D(0) <= rate <= D(1)


@given(outcomes, st.integers(min_value=1, max_value=10_000))
def test_scaling_every_hedge_rank_leaves_the_rate_unchanged(items, k):
    scaled = {h: r * k for h, r in HEDGE_RANK.items()}
    assert delivery_rate(items, ranks=scaled).rate == delivery_rate(items).rate


@given(outcomes, st.decimals(min_value=D("0.001"), max_value=D("1000"), places=3))
def test_scaling_by_a_decimal_factor_changes_nothing_either(items, k):
    scaled = {h: D(r) * k for h, r in HEDGE_RANK.items()}
    assert delivery_rate(items, ranks=scaled).rate == delivery_rate(items).rate


def _swap(item: ClaimOutcome) -> ClaimOutcome:
    flipped = {Status.MET: Status.MISSED, Status.MISSED: Status.MET}
    return ClaimOutcome(flipped.get(item.status, item.status), item.hedge, item.silent)


@given(outcomes)
def test_swapping_met_and_missed_gives_one_minus_the_rate(items):
    rate, swapped = delivery_rate(items).rate, delivery_rate([_swap(i) for i in items]).rate
    if rate is None:
        assert swapped is None
    else:
        assert abs(swapped - (D(1) - rate)) <= D("1e-26")


@given(outcomes)
def test_the_counts_partition_the_outcomes(items):
    r = delivery_rate(items)
    assert r.met + r.missed + r.open + r.unresolvable + r.silent == len(items)


# --------------------------------------------------------------------------- #
# grade monotonicity
# --------------------------------------------------------------------------- #

amount = st.decimals(min_value=D("-1e6"), max_value=D("1e6"), places=4, allow_nan=False, allow_infinity=False)


@given(amount, amount, amount)
def test_raising_the_actual_never_turns_a_floor_claim_from_met_to_missed(low, a, b):
    lo_actual, hi_actual = sorted((a, b))
    if grade(floor(low), lo_actual, Unit.PERCENT, DEFAULT_RULE)[0] is Status.MET:
        assert grade(floor(low), hi_actual, Unit.PERCENT, DEFAULT_RULE)[0] is Status.MET


@given(amount, amount, amount)
def test_lowering_the_actual_never_turns_a_ceiling_claim_from_met_to_missed(high, a, b):
    lo_actual, hi_actual = sorted((a, b))
    if grade(ceiling(high), hi_actual, Unit.PERCENT, DEFAULT_RULE)[0] is Status.MET:
        assert grade(ceiling(high), lo_actual, Unit.PERCENT, DEFAULT_RULE)[0] is Status.MET


@given(amount, amount, amount)
def test_a_range_is_met_exactly_when_the_actual_is_between_its_ends(a, b, actual):
    low, high = sorted((a, b))
    status = grade(rng(low, high), actual, Unit.PERCENT, DEFAULT_RULE)[0]
    assert (status is Status.MET) == (low <= actual <= high)


@given(amount)
def test_a_point_is_always_met_by_itself(v):
    assert grade(point(v), v, Unit.PERCENT, DEFAULT_RULE)[0] is Status.MET


# --------------------------------------------------------------------------- #
# SILENT
# --------------------------------------------------------------------------- #

KEY = period_key("FY27")
OTHER_KEY = period_key("FY28")


def call(day: int, *mentions, clean=True) -> TranscriptMentions:
    return TranscriptMentions(date(2026, 6, day), frozenset(mentions), clean)


def silent(later, *, resolved=False, metric="revenue", key=KEY, rule=DEFAULT_RULE):
    return detect_silent(metric, key, later, resolved=resolved, rule=rule)


def test_the_default_window_is_two_calls():
    assert DEFAULT_RULE.consecutive_calls == 2


def test_exactly_n_clean_later_calls_without_a_mention_is_silent():
    assert silent([call(1), call(2)]) is True


def test_one_call_fewer_than_n_is_not_silent():
    assert silent([call(1)]) is False
    assert silent([]) is False


def test_only_the_next_n_calls_are_looked_at():
    assert silent([call(1), call(2), call(3, ("revenue", KEY))]) is True


def test_a_mention_in_the_window_breaks_it():
    assert silent([call(1, ("revenue", KEY)), call(2)]) is False
    assert silent([call(1), call(2, ("revenue", KEY))]) is False


def test_a_mention_of_another_metric_or_another_period_does_not_break_it():
    assert silent([call(1, ("pat", KEY)), call(2, ("revenue", OTHER_KEY))]) is True


def test_a_transcript_with_quarantined_claims_cannot_establish_silence():
    """The claim may be in the quarantine: absence is ambiguous, so not silent."""
    assert silent([call(1), call(2, clean=False)]) is False
    assert silent([call(1, clean=False), call(2)]) is False


def test_a_dirty_transcript_beyond_the_window_does_not_matter():
    assert silent([call(1), call(2), call(3, clean=False)]) is True


def test_a_resolvable_period_is_never_silent():
    assert silent([call(1), call(2), call(3)], resolved=True) is False


def test_later_calls_are_read_in_call_date_order_whatever_order_they_arrive_in():
    assert silent([call(9, ("revenue", KEY)), call(1), call(2)]) is True  # the mention is third by date
    assert silent([call(2), call(9), call(1, ("revenue", KEY))]) is False  # and here it is first


def test_the_window_is_a_rule_parameter():
    rule = ResolutionRule(consecutive_calls=3)
    assert silent([call(1), call(2)], rule=rule) is False
    assert silent([call(1), call(2), call(3)], rule=rule) is True


def test_an_unparsed_period_is_keyed_on_its_label():
    key = period_key("Medium   Term")
    assert silent([call(1), call(2)], key=key) is True
    assert silent([call(1, ("revenue", "medium term")), call(2)], key=key) is False


def test_a_resolved_claim_is_one_with_a_grade_or_an_answer_already_public():
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    graded_ = resolve_claim(claim("revenue", "FY27", 1000), facts)
    open_ = resolve_claim(claim("revenue", "FY27", 1000), [])
    known = resolve_claim(claim("revenue", "FY27", 1000), facts, facts_when_made=facts)
    directional = resolve_claim(claim("revenue", "FY27", None, None), facts)
    unmapped = resolve_claim(claim("roe", "FY27", 15, unit=Unit.PERCENT), facts)
    assert [period_resolved(r) for r in (graded_, open_, known, directional, unmapped)] == [
        True, False, True, False, False,
    ]  # fmt: skip


@given(st.lists(st.tuples(st.integers(1, 28), st.booleans(), st.booleans()), max_size=6), st.booleans())
def test_silence_needs_a_full_clean_window_and_an_unresolved_period(spec, resolved):
    later = [call(day, *([("revenue", KEY)] if mentioned else []), clean=clean) for day, mentioned, clean in spec]
    ordered = sorted(later, key=lambda t: t.call_date)[:2]
    expected = (
        not resolved
        and len(ordered) == 2
        and all(t.clean and ("revenue", KEY) not in t.mentions for t in ordered)
    )
    assert silent(later, resolved=resolved) is expected


# --------------------------------------------------------------------------- #
# select_claims
# --------------------------------------------------------------------------- #


def test_the_first_claim_per_metric_and_period_is_the_original_promise():
    """Grading only the last reiteration would let a target be lowered and then met."""
    original = claim("revenue", "FY27", 1200)
    lowered = claim("revenue", "FY 2026-27", 1000)  # same period, other spelling
    other_period = claim("revenue", "FY28", 1500)
    other_metric = claim("pat", "FY27", 100)
    claims = [original, other_metric, lowered, other_period]
    assert select_claims(claims) == [original, other_metric, other_period]
    assert select_claims(claims, "first") == [original, other_metric, other_period]


def test_the_last_claim_is_available_for_comparison():
    original = claim("revenue", "FY27", 1200)
    lowered = claim("revenue", "FY27", 1000)
    assert select_claims([original, lowered], "last") == [lowered]


def test_unparsed_labels_group_on_their_normalised_text():
    a, b = claim("revenue", "Medium Term", 1), claim("revenue", "medium  term", 2)
    assert select_claims([a, b]) == [a]


def test_selection_does_not_reorder_or_drop_a_group_of_one():
    a, b, c = claim("revenue", "FY27", 1), claim("pat", "FY27", 2), claim("pat", "FY28", 3)
    assert select_claims([a, b, c]) == [a, b, c]
    assert select_claims([]) == []


def test_an_unknown_basis_is_refused():
    with pytest.raises(ValueError):
        select_claims([claim("revenue", "FY27", 1)], "middle")  # type: ignore[arg-type]


def test_selection_leaves_every_claim_individually_resolvable():
    """This only chooses which claims feed the rate; the dropped ones still grade."""
    facts = [duration(ratios.REVENUE, FY27, crore(1000))]
    original, lowered = claim("revenue", "FY27", 1200), claim("revenue", "FY27", 1000)
    assert resolve_claim(original, facts).status is Status.MISSED
    assert resolve_claim(lowered, facts).status is Status.MET
    kept = select_claims([original, lowered])
    assert delivery_rate([ClaimOutcome(resolve_claim(c, facts).status, c.hedge) for c in kept]).rate == D(0)


def test_a_negative_capex_is_bad_input_not_repaired():
    item = "purchase_of_property_plant_and_equipment_classified_as_investing_activities"
    res = resolved("capex", "FY27", [duration(item, FY27, crore(-5))])
    assert (res.status, res.reason) == (Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING)
