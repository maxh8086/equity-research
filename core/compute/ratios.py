"""Financial ratios from reported line items: margins, debt ratios, ROCE.

Pure: callers pass the line items in, already read at `t`
(core.db.pit.ratios_as_of). Ratios are fractions (0.237, not 23.7%).

Which line items feed which ratio is a research decision, fixed here under
RULE_VERSION and checked against Screener in Session 6. A change of definition
is a new RULE_VERSION, never an edit of what an old one meant.

A ratio whose denominator is zero or negative is undefined and returns None:
interest cover with no finance costs is not infinite, and a margin on negative
revenue or a return on negative capital means nothing. None is never a zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

RULE_VERSION = "ratios/1"

# Division rounds to this many significant digits, whatever the caller's context.
PRECISION = 28

# Research decision (ratios/1): these definitions hold for non-financial
# companies filing under the Ind AS taxonomy. For NBFCs, banks and insurers,
# finance costs are the cost of the product, so EBIT, interest cover and ROCE
# say nothing; their ratios (ROE, NIM, cost to income) come in a later rule.
# Values are core.db.models.XbrlTaxonomy values; this module imports no models.
APPLICABLE_FAMILIES = frozenset({"ind_as"})

# Incremental ROCE compares a fiscal year with the one this many years before.
INCREMENTAL_ROCE_YEARS = 3

# Line items (ingest/nse_xbrl/mapping.py, Ind AS 2020 taxonomy).
REVENUE = "revenue_from_operations"
TOTAL_EXPENSES = "expenses"  # includes finance costs and depreciation
FINANCE_COSTS = "finance_costs"
DEPRECIATION = "depreciation_depletion_and_amortisation_expense"
PROFIT_BEFORE_TAX = "profit_before_tax"  # after exceptional items
PROFIT = "profit_loss_for_period"  # before non-controlling interests
EQUITY = "equity"  # total equity, including non-controlling interests
BORROWINGS_CURRENT = "borrowings_current"
BORROWINGS_NONCURRENT = "borrowings_noncurrent"
CASH = "cash_and_cash_equivalents"
OTHER_BANK_BALANCES = "bank_balance_other_than_cash_and_cash_equivalents"
CURRENT_INVESTMENTS = "current_investments"

DURATION_LINE_ITEMS = (REVENUE, TOTAL_EXPENSES, FINANCE_COSTS, DEPRECIATION, PROFIT_BEFORE_TAX, PROFIT)
INSTANT_LINE_ITEMS = (
    EQUITY, BORROWINGS_CURRENT, BORROWINGS_NONCURRENT, CASH, OTHER_BANK_BALANCES, CURRENT_INVESTMENTS,
)  # fmt: skip


class Ratio(StrEnum):
    OPERATING_MARGIN = "operating_margin"  # any reported period
    EBIT_MARGIN = "ebit_margin"
    NET_MARGIN = "net_margin"
    INTEREST_COVERAGE = "interest_coverage"
    DEBT_TO_EQUITY = "debt_to_equity"  # any balance-sheet date
    NET_DEBT_TO_EQUITY = "net_debt_to_equity"
    ROCE = "roce"  # fiscal years only
    INCREMENTAL_ROCE = "incremental_roce"


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if not denominator > 0:
        return None
    with localcontext(prec=PRECISION):
        return numerator / denominator


def _not_negative(**values: Decimal) -> None:
    for name, value in values.items():
        if value < 0:
            raise ValueError(f"{name} must not be negative, got {value}")


def shift_years(day: date, years: int) -> date:
    """The same calendar day `years` later (or earlier); 29 February falls back to the 28th."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def is_fiscal_year(start: date, end: date) -> bool:
    """Whether [start, end] spans exactly twelve months (1 Apr 2023 - 31 Mar 2024)."""
    return end == shift_years(start, 1) - timedelta(days=1)


def prior_balance_date(period_start: date) -> date:
    """The balance-sheet date that opens a period: the day before it starts."""
    return period_start - timedelta(days=1)


def ebit(profit_before_tax: Decimal, finance_costs: Decimal) -> Decimal:
    """Earnings before interest and tax: profit before tax with finance costs added back."""
    _not_negative(finance_costs=finance_costs)
    return profit_before_tax + finance_costs


def operating_profit(
    revenue: Decimal, total_expenses: Decimal, finance_costs: Decimal, depreciation: Decimal
) -> Decimal:
    """Revenue from operations less expenses other than finance costs and depreciation.

    Other income and exceptional items are excluded (neither is in revenue
    from operations nor in expenses).
    """
    _not_negative(finance_costs=finance_costs, depreciation=depreciation)
    return revenue - (total_expenses - finance_costs - depreciation)


def margin(profit: Decimal, revenue: Decimal) -> Decimal | None:
    """`profit` as a fraction of revenue; undefined unless revenue is positive."""
    return _divide(profit, revenue)


def total_borrowings(current: Decimal, noncurrent: Decimal) -> Decimal:
    _not_negative(current=current, noncurrent=noncurrent)
    return current + noncurrent


def net_debt(
    borrowings: Decimal, cash: Decimal, other_bank_balances: Decimal, current_investments: Decimal
) -> Decimal:
    """Borrowings less cash, other bank balances and current investments; negative means net cash."""
    _not_negative(
        borrowings=borrowings, cash=cash, other_bank_balances=other_bank_balances,
        current_investments=current_investments,
    )  # fmt: skip
    return borrowings - cash - other_bank_balances - current_investments


def debt_to_equity(debt: Decimal, equity: Decimal) -> Decimal | None:
    """Debt (gross or net) over total equity; undefined unless equity is positive."""
    return _divide(debt, equity)


def interest_coverage(ebit_value: Decimal, finance_costs: Decimal) -> Decimal | None:
    """EBIT over finance costs; undefined when there are no finance costs."""
    _not_negative(finance_costs=finance_costs)
    return _divide(ebit_value, finance_costs)


def capital_employed(equity: Decimal, borrowings: Decimal) -> Decimal:
    """Total equity plus borrowings. Lease liabilities are not tagged separately in results XBRL."""
    _not_negative(borrowings=borrowings)
    return equity + borrowings


def average_balance(opening: Decimal, closing: Decimal) -> Decimal:
    with localcontext(prec=PRECISION):
        return (opening + closing) / 2


def roce(ebit_value: Decimal, average_capital_employed: Decimal) -> Decimal | None:
    """EBIT for a fiscal year over the average of opening and closing capital employed."""
    return _divide(ebit_value, average_capital_employed)


def incremental_roce(
    ebit_now: Decimal, ebit_before: Decimal, capital_now: Decimal, capital_before: Decimal
) -> Decimal | None:
    """Extra EBIT per rupee of extra capital employed between two fiscal year ends.

    Undefined unless capital employed grew: when it shrank, the sign of the
    ratio would invert its meaning.
    """
    return _divide(ebit_now - ebit_before, capital_now - capital_before)
