"""Financial ratios: pure, property-tested."""

from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from core.compute import ratios as r

D = Decimal

money = st.decimals(min_value=D("-1e13"), max_value=D("1e13"), places=2)
positive = st.decimals(min_value=D("0.01"), max_value=D("1e13"), places=2)
non_negative = st.decimals(min_value=D(0), max_value=D("1e13"), places=2)
scale = st.decimals(min_value=D("0.01"), max_value=D("1000"), places=2)
days = st.dates(min_value=date(1990, 1, 1), max_value=date(2100, 12, 31))

# INFY FY24 consolidated, as filed (tests/fixtures/nse_xbrl, INDAS_104589_...)
INFY = dict(
    revenue=D("1536700000000.00"),
    expenses=D("1223930000000.00"),
    finance_costs=D("4700000000.00"),
    depreciation=D("46780000000.00"),
    pbt=D("359880000000.00"),
    profit=D("262480000000.00"),
)


def close(a: Decimal, b: Decimal, digits: int = 20) -> bool:
    """Equal to `digits` significant figures: division rounds at 28."""
    with localcontext(prec=digits):
        return +a == +b


# --------------------------------------------------------------------------- #
# Worked examples
# --------------------------------------------------------------------------- #


def test_infosys_fy24_operating_margin_matches_hand_calculation():
    # 1,53,670 - (1,22,393 - 470 - 4,678) = 36,425 crore
    op = r.operating_profit(INFY["revenue"], INFY["expenses"], INFY["finance_costs"], INFY["depreciation"])
    assert op == D("364250000000.00")
    assert round(r.margin(op, INFY["revenue"]), 4) == D("0.2370")


def test_infosys_fy24_ebit_net_margin_and_interest_cover():
    ebit = r.ebit(INFY["pbt"], INFY["finance_costs"])
    assert ebit == D("364580000000.00")
    assert round(r.margin(ebit, INFY["revenue"]), 4) == D("0.2372")
    assert round(r.margin(INFY["profit"], INFY["revenue"]), 4) == D("0.1708")
    assert round(r.interest_coverage(ebit, INFY["finance_costs"]), 2) == D("77.57")


def test_roce_on_average_capital_employed():
    # EBIT 120 on capital 900 opening and 1,100 closing: 120 / 1,000
    opening = r.capital_employed(D(600), r.total_borrowings(D(100), D(200)))
    closing = r.capital_employed(D(800), r.total_borrowings(D(100), D(200)))
    assert r.roce(r.ebit(D(100), D(20)), r.average_balance(opening, closing)) == D("0.12")


def test_debt_ratios():
    debt = r.total_borrowings(D(280), D(713))
    assert r.debt_to_equity(debt, D(905)) == D(993) / D(905)
    net = r.net_debt(debt, D(87), D(12), D(5))
    assert net == D(889)
    assert r.debt_to_equity(r.net_debt(D(0), D(148), D(0), D(129)), D(885)) == D(-277) / D(885)  # net cash


def test_incremental_roce():
    # EBIT 100 -> 160 while capital employed 1,000 -> 1,300: 60 / 300
    assert r.incremental_roce(D(160), D(100), D(1300), D(1000)) == D("0.2")


# --------------------------------------------------------------------------- #
# Undefined, never zero or infinite
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "call",
    [
        lambda: r.margin(D(10), D(0)),
        lambda: r.margin(D(10), D(-5)),
        lambda: r.interest_coverage(D(10), D(0)),
        lambda: r.debt_to_equity(D(10), D(0)),
        lambda: r.debt_to_equity(D(10), D(-1)),
        lambda: r.roce(D(10), D(0)),
        lambda: r.roce(D(10), D(-100)),
        lambda: r.incremental_roce(D(20), D(10), D(100), D(100)),
        lambda: r.incremental_roce(D(20), D(10), D(90), D(100)),  # capital shrank
    ],
)
def test_non_positive_denominator_is_undefined(call):
    assert call() is None


@pytest.mark.parametrize(
    "call",
    [
        lambda: r.ebit(D(10), D(-1)),
        lambda: r.operating_profit(D(10), D(5), D(-1), D(0)),
        lambda: r.operating_profit(D(10), D(5), D(0), D(-1)),
        lambda: r.total_borrowings(D(-1), D(0)),
        lambda: r.net_debt(D(0), D(-1), D(0), D(0)),
        lambda: r.capital_employed(D(10), D(-1)),
        lambda: r.interest_coverage(D(10), D(-1)),
    ],
)
def test_negative_costs_and_balances_are_rejected(call):
    with pytest.raises(ValueError):
        call()


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #


def test_fiscal_year_detection():
    assert r.is_fiscal_year(date(2023, 4, 1), date(2024, 3, 31))
    assert r.is_fiscal_year(date(2024, 1, 1), date(2024, 12, 31))
    assert not r.is_fiscal_year(date(2024, 1, 1), date(2024, 3, 31))  # a quarter
    assert not r.is_fiscal_year(date(2024, 4, 1), date(2024, 9, 30))  # half-year to date
    assert not r.is_fiscal_year(date(2023, 4, 1), date(2024, 4, 1))


def test_shift_years_falls_back_from_leap_day():
    assert r.shift_years(date(2024, 2, 29), -1) == date(2023, 2, 28)
    assert r.shift_years(date(2024, 3, 31), -3) == date(2021, 3, 31)


@given(days)
def test_twelve_months_from_any_start_is_a_fiscal_year(start):
    end = r.shift_years(start, 1) - timedelta(days=1)
    assert r.is_fiscal_year(start, end)
    assert not r.is_fiscal_year(start, end + timedelta(days=1))
    assert not r.is_fiscal_year(start, end - timedelta(days=1))


@given(days)
def test_opening_balance_is_the_day_before(start):
    assert r.prior_balance_date(start) < start
    assert r.prior_balance_date(start) + timedelta(days=1) == start


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #


@given(money, positive, positive, non_negative, non_negative, money, scale)
def test_margins_are_unchanged_by_units(pbt, revenue, expenses, finance, dep, profit, k):
    """Crore or rupees: the same filing gives the same ratios."""
    assume(expenses >= finance + dep)
    base = (
        r.margin(r.operating_profit(revenue, expenses, finance, dep), revenue),
        r.margin(r.ebit(pbt, finance), revenue),
        r.margin(profit, revenue),
    )
    scaled = (
        r.margin(r.operating_profit(revenue * k, expenses * k, finance * k, dep * k), revenue * k),
        r.margin(r.ebit(pbt * k, finance * k), revenue * k),
        r.margin(profit * k, revenue * k),
    )
    assert all(close(a, b) for a, b in zip(base, scaled, strict=True))


@given(money, positive)
def test_margin_times_revenue_recovers_profit(profit, revenue):
    assert close(r.margin(profit, revenue) * revenue, profit)


@given(money, non_negative, positive, positive, non_negative, non_negative)
def test_roce_times_average_capital_is_ebit(pbt, finance, eq_open, eq_close, debt_open, debt_close):
    avg = r.average_balance(r.capital_employed(eq_open, debt_open), r.capital_employed(eq_close, debt_close))
    ebit = r.ebit(pbt, finance)
    assert close(r.roce(ebit, avg) * avg, ebit)


@given(non_negative, non_negative)
def test_average_lies_between_opening_and_closing(opening, closing):
    avg = r.average_balance(opening, closing)
    assert min(opening, closing) <= avg <= max(opening, closing)
    assert avg == r.average_balance(closing, opening)


@given(non_negative, non_negative, non_negative, non_negative, positive)
def test_net_debt_ratio_never_exceeds_gross(debt, cash, bank, investments, equity):
    net = r.net_debt(debt, cash, bank, investments)
    assert net <= debt
    assert r.debt_to_equity(net, equity) <= r.debt_to_equity(debt, equity)
    assert r.debt_to_equity(debt, equity) >= 0


@given(money, positive)
def test_interest_cover_rises_with_profit(pbt, finance):
    ebit = r.ebit(pbt, finance)
    assert r.interest_coverage(r.ebit(pbt + 1, finance), finance) > r.interest_coverage(ebit, finance)
    # Profit before tax of zero means EBIT exactly covers interest once.
    assert r.interest_coverage(r.ebit(D(0), finance), finance) == 1


@given(money, money, positive, positive, scale)
def test_incremental_roce_is_unchanged_by_units(ebit_now, ebit_before, capital_before, growth, k):
    capital_now = capital_before + growth
    base = r.incremental_roce(ebit_now, ebit_before, capital_now, capital_before)
    scaled = r.incremental_roce(ebit_now * k, ebit_before * k, capital_now * k, capital_before * k)
    assert close(base, scaled)


@given(money, positive)
def test_incremental_roce_equals_roce_from_zero(ebit_now, capital):
    """From nothing, the incremental return is the plain return on closing capital."""
    assert r.incremental_roce(ebit_now, D(0), capital, D(0)) == r.roce(ebit_now, capital)


@given(money, positive)
def test_division_ignores_the_callers_context(profit, revenue):
    with localcontext(prec=5):
        low = r.margin(profit, revenue)
    assert low == r.margin(profit, revenue)
