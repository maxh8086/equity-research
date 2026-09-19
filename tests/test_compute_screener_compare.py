"""Screener comparison: line pairs, blanks, alternatives, ratios through the ratios/1 formulas. No database."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from core.compute import ratios as r
from core.compute import screener_compare as sc
from core.compute.screener_compare import CRORE, Status
from ingest.screener_export import parser
from ingest.screener_export.parser import parse_export

FY22, FY21, FY19 = date(2022, 3, 31), date(2021, 3, 31), date(2019, 3, 31)
FIXTURE = Path(__file__).parent / "fixtures" / "screener_export" / "vinati_data_sheet_v2_1.xlsx"

money = st.decimals(min_value=Decimal("-1e13"), max_value=Decimal("1e13"), places=2, allow_nan=False)


def crore(x: str) -> Decimal:
    return Decimal(x) * CRORE


def _vinati() -> dict:
    return {(v.statement, v.line, v.period_end): v.value for v in parse_export(FIXTURE.read_bytes()).values}


def _screener(end: date, **lines: str) -> dict:
    bs = set(sc.BS_LINES)
    return {("bs" if k in bs else "pl", k, end): crore(v) for k, v in lines.items()}


def _by_name(comparisons) -> dict:
    return {c.name: c for c in comparisons}


def test_line_lists_agree_with_the_parser():
    by_statement = {s.statement: tuple(line for line, *_ in s.lines) for s in parser.SECTIONS}
    assert by_statement["pl"] == sc.PL_LINES and by_statement["bs"] == sc.BS_LINES


def test_screener_year_derives_vinati_fy22():
    y = sc.screener_year(_vinati(), FY22)
    assert y["operating_profit"] == crore("435.48")
    assert y["equity"] == y["share_capital"] + y["reserves"]
    assert y["ebit"] == y["pbt"] + y["interest"]
    assert y["capital_employed"] == y["equity"] + y["borrowings"]


def test_blank_in_a_reported_period_is_zero_and_an_unreported_period_is_absent():
    values = _screener(FY22, sales="100", pbt="10", share_capital="5", reserves="45")
    y = sc.screener_year(values, FY22)
    assert y["interest"] == 0 and y["borrowings"] == 0 and y["operating_profit"] == crore("100")
    assert sc.screener_year(values, FY21) == {}


def test_matching_ind_as_year():
    screener = _screener(FY22, sales="1000", raw_material="600", employee="100", other_exp="50", depreciation="40",
                         interest="10", other_income="5", pbt="205", net_profit="150", share_capital="10",
                         reserves="990", borrowings="200", cash_bank="30", cwip="0")  # fmt: skip
    ours = {(k, FY22): crore(v) for k, v in {
        r.REVENUE: "1000", r.TOTAL_EXPENSES: "800", r.FINANCE_COSTS: "10", r.DEPRECIATION: "40",
        "other_income": "5", r.PROFIT_BEFORE_TAX: "205", r.PROFIT: "150.4", r.EQUITY: "1000",
        r.BORROWINGS_NONCURRENT: "200", r.CASH: "30",
    }.items()}  # fmt: skip
    got = _by_name(sc.compare_lines("ind_as", screener, ours, FY22))
    assert {c.status for c in got.values()} == {Status.MATCH}
    assert got["operating_profit"].ours == crore("250")
    # Optional terms absent count as zero, and are named.
    assert got["borrowings"].sides[0].absent == (r.BORROWINGS_CURRENT,)
    # CWIP absent from the filing, nil on Screener.
    assert got["cwip"].ours == 0
    assert sc.gate_passes(list(got.values()))


def test_owners_only_profit_is_recorded_as_the_alternative():
    screener = _screener(FY22, sales="100", net_profit="80")
    ours = {(r.PROFIT, FY22): crore("100"), ("profit_or_loss_attributable_to_owners_of_parent", FY22): crore("80")}
    c = _by_name(sc.compare_lines("ind_as", screener, ours, FY22))["net_profit"]
    assert c.status is Status.MATCH_ALTERNATE
    assert [s.within for s in c.sides] == [False, True]
    assert c.difference == crore("20")


def test_divergence_and_missing_input_fail_the_gate():
    screener = _screener(FY22, sales="100", pbt="10")
    ours = {(r.REVENUE, FY22): crore("110")}
    got = _by_name(sc.compare_lines("ind_as", screener, ours, FY22))
    assert got["sales"].status is Status.DIVERGE
    assert got["pbt"].status is Status.MISSING_OURS
    assert got["operating_profit"].sides[0].absent == (r.TOTAL_EXPENSES, r.FINANCE_COSTS, r.DEPRECIATION)
    assert got["equity"].status is Status.MISSING_SCREENER
    assert not sc.gate_passes(list(got.values()))


def test_ungated_pairs_never_decide_the_gate():
    screener = _screener(FY22, sales="100")
    comparisons = sc.compare_lines("life_insurance", screener, {}, FY22)
    assert all(not c.gate for c in comparisons)
    assert not sc.gate_passes(comparisons)  # nothing gated: no evidence, no pass


def test_nbfc_borrowings_sum_debt_securities_and_deposits():
    screener = _screener(FY22, share_capital="1", reserves="9", borrowings="300")
    ours = {("debt_securities", FY22): crore("100"), ("borrowings", FY22): crore("150"),
            ("deposits", FY22): crore("50")}  # fmt: skip
    c = _by_name(sc.compare_lines("nbfc", screener, ours, FY22))["borrowings"]
    assert c.status is Status.MATCH and c.sides[0].absent == ("subordinated_liabilities",)


def test_bank_pairs():
    screener = _screener(FY22, sales="500", interest="300", pbt="80", net_profit="60", share_capital="10",
                         reserves="490", borrowings="5000")  # fmt: skip
    ours = {(k, FY22): crore(v) for k, v in {
        "interest_earned": "500", "interest_expended": "300", "profit_loss_from_ordinary_activities_before_tax": "80",
        "profit_loss_for_the_period": "60", "capital": "10", "reserves_and_surplus": "490", "deposits": "4500",
        "borrowings": "500",
    }.items()}  # fmt: skip
    assert {c.status for c in sc.compare_lines("bank", screener, ours, FY22)} == {Status.MATCH}


@given(money)
def test_tolerance_is_at_least_the_floor_and_one_percent(value):
    t = sc.line_tolerance(value)
    assert t >= sc.LINE_ABSOLUTE and t >= abs(value) / 100
    assert sc.within(value + t, value, t) and not sc.within(value + t + Decimal("0.01"), value, t)


@given(money, money)
def test_optional_sum_is_exact_and_order_free(a, b):
    ours = {("x", FY22): a, ("y", FY22): b}
    one = sc.evaluate_sum(sc.Sum("s", sc._optional("x", "y")), ours, FY22)
    two = sc.evaluate_sum(sc.Sum("s", sc._optional("y", "x")), ours, FY22)
    assert one[0] == two[0] == a + b


# --------------------------------------------------------------------------- #
# Ratios
# --------------------------------------------------------------------------- #


def _years(values_by_year: dict[date, dict[str, str]]) -> dict:
    out: dict = {}
    for end, lines in values_by_year.items():
        out |= _screener(end, **lines)
    return out


def test_screener_ratios_use_ratios_1_formulas():
    screener = _years({
        FY19: dict(sales="800", pbt="80", interest="10", share_capital="10", reserves="590", borrowings="100"),
        FY21: dict(sales="900", pbt="100", interest="10", share_capital="10", reserves="690", borrowings="100"),
        FY22: dict(sales="1000", raw_material="700", pbt="150", interest="10", net_profit="110", share_capital="10",
                   reserves="890", borrowings="100", cash_bank="40"),
    })  # fmt: skip
    years = {d: sc.screener_year(screener, d) for d in (FY19, FY21, FY22)}
    ratio = lambda name: sc.screener_ratio(name, years, FY22)  # noqa: E731
    assert ratio(r.Ratio.OPERATING_MARGIN) == Decimal("0.3")
    assert ratio(r.Ratio.EBIT_MARGIN) == Decimal("0.16")
    assert ratio(r.Ratio.INTEREST_COVERAGE) == 16
    assert ratio(r.Ratio.DEBT_TO_EQUITY) == Decimal("0.1111111111111111111111111111")
    assert ratio(r.Ratio.ROCE) == r.roce(crore("160"), r.average_balance(crore("800"), crore("1000")))
    assert ratio(sc.ROCE_CLOSING) == Decimal("0.16")
    assert ratio(r.Ratio.INCREMENTAL_ROCE) == r.incremental_roce(crore("160"), crore("90"), crore("1000"), crore("700"))


def test_ratio_statuses():
    screener = _years({FY22: dict(sales="1000", raw_material="700", pbt="150", interest="0", net_profit="110",
                                  share_capital="10", reserves="990")})  # fmt: skip
    ours_ratios = {
        (r.Ratio.OPERATING_MARGIN, FY22): (Decimal("0.302"), None),
        (r.Ratio.NET_MARGIN, FY22): (Decimal("0.13"), None),
        (r.Ratio.INTEREST_COVERAGE, FY22): (None, "undefined"),
    }
    got = _by_name(sc.compare_ratios(screener, ours_ratios, {}, FY22))
    assert got["operating_margin"].status is Status.MATCH
    assert got["net_margin"].status is Status.DIVERGE
    assert got["interest_coverage"].status is Status.MATCH  # no interest: undefined on both sides
    assert got["ebit_margin"].status is Status.MISSING_OURS
    assert got["roce"].status is Status.MISSING_SCREENER  # no prior year on Screener
    assert not got["net_debt_to_equity"].gate and not got[sc.ROCE_CLOSING].gate


def test_closing_roce_of_ours():
    ours = {(r.PROFIT_BEFORE_TAX, FY22): crore("90"), (r.FINANCE_COSTS, FY22): crore("10"),
            (r.EQUITY, FY22): crore("400"), (r.BORROWINGS_CURRENT, FY22): crore("100")}  # fmt: skip
    assert sc.ours_closing_roce(ours, FY22) == (Decimal("0.2"), None)
    assert sc.ours_closing_roce({}, FY22) == (None, "missing_input")
