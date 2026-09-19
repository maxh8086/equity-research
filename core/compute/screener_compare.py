"""Compare our fiscal-year line items and ratios with a Screener.in export (Session 6).

Pure: callers pass in Screener's values and our facts and ratios, each already
read at `t`. Screener is a reference to check our parsing and definitions
against, never an input to anything else.

Research decisions, fixed under COMPARE_RULE_VERSION:

- A blank Screener cell in a period Screener reports (any value in that
  statement for that date) counts as zero. Screener leaves nil lines blank.
- A line is matched with a sum of our XBRL line items. In a sum, a term
  marked optional that is absent counts as zero (filings omit nil items); a
  required term that is absent leaves the sum missing. When every term is
  absent and Screener shows zero, the line matches.
- A pair may name alternatives after its primary sum, e.g. profit
  attributable to owners beside profit including non-controlling interests.
  The comparison records which sums Screener agrees with. That answers what
  Screener's line contains; it does not change our definitions.
- Ratios are computed from Screener's lines with the same ratios/1 functions
  that compute ours, and compared with ours as read at `t`.
- Pairs marked `gate=False` are reported but do not decide the Week 2 gate:
  their meaning on Screener's side is not established yet.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from core.compute import ratios as r

COMPARE_RULE_VERSION = "screener_compare/1"

CRORE = Decimal(10) ** 7

# Line items: within 1% of Screener's value or 0.05 crore (Screener rounds to 0.01 crore).
LINE_RELATIVE = Decimal("0.01")
LINE_ABSOLUTE = Decimal("0.05") * CRORE
# Ratios are fractions.
MARGIN_TOLERANCE = Decimal("0.005")
LEVERAGE_TOLERANCE = Decimal("0.02")
COVERAGE_RELATIVE = Decimal("0.02")
COVERAGE_ABSOLUTE = Decimal("0.05")


class Status(StrEnum):
    MATCH = "match"  # the primary sum agrees
    MATCH_ALTERNATE = "match_alternate"  # only an alternative sum agrees
    DIVERGE = "diverge"
    MISSING_SCREENER = "missing_screener"
    MISSING_OURS = "missing_ours"


FAILING = frozenset({Status.DIVERGE, Status.MISSING_SCREENER, Status.MISSING_OURS})


@dataclass(frozen=True)
class Term:
    item: str
    sign: int = 1
    optional: bool = False


@dataclass(frozen=True)
class Sum:
    label: str
    terms: tuple[Term, ...]


@dataclass(frozen=True)
class LinePair:
    line: str  # a Screener line, or one derived in screener_year
    ours: tuple[Sum, ...]  # primary first, then alternatives
    gate: bool = True
    note: str = ""


@dataclass(frozen=True)
class SideResult:
    label: str
    value: Decimal | None
    within: bool
    absent: tuple[str, ...]  # optional terms counted as zero, or the terms that left the sum missing


@dataclass(frozen=True)
class Comparison:
    kind: str  # line | ratio
    name: str
    period_end: date
    status: Status
    screener: Decimal | None
    ours: Decimal | None  # the primary sum, or our ratio
    tolerance: Decimal
    gate: bool
    sides: tuple[SideResult, ...]  # every sum of ours, primary first (lines only)
    screener_inputs: tuple[tuple[str, Decimal | None], ...]
    note: str = ""

    @property
    def difference(self) -> Decimal | None:
        """Ours minus Screener's."""
        if self.ours is None or self.screener is None:
            return None
        return self.ours - self.screener


def _one(item: str) -> tuple[Term, ...]:
    return (Term(item),)


def _optional(*items: str) -> tuple[Term, ...]:
    return tuple(Term(i, optional=True) for i in items)


OPERATING_PROFIT_TERMS = (
    Term(r.REVENUE), Term(r.TOTAL_EXPENSES, -1), Term(r.FINANCE_COSTS), Term(r.DEPRECIATION),
)  # fmt: skip
EBIT_TERMS = (Term(r.PROFIT_BEFORE_TAX), Term(r.FINANCE_COSTS))
CAPITAL_TERMS = (Term(r.EQUITY), *_optional(r.BORROWINGS_CURRENT, r.BORROWINGS_NONCURRENT))

_LEASE_NOTE = "lease liabilities are not tagged in results XBRL: a Screener excess may be leases"
_INSURER_NOTE = "insurer mapping not established: reported, not gated"


def _borrowings(label: str, *items: str) -> LinePair:
    return LinePair("borrowings", (Sum(label, _optional(*items)),), note=_LEASE_NOTE)


IND_AS_PAIRS = (
    LinePair("sales", (Sum("revenue_from_operations", _one(r.REVENUE)),)),
    LinePair("operating_profit", (Sum("revenue - (expenses - finance_costs - depreciation)", OPERATING_PROFIT_TERMS),),
             note="other income is excluded on both sides"),  # fmt: skip
    LinePair("other_income", (Sum("other_income", _one("other_income")),), gate=False,
             note="Screener may move exceptional items here"),  # fmt: skip
    LinePair("depreciation", (Sum("depreciation", _one(r.DEPRECIATION)),)),
    LinePair("interest", (Sum("finance_costs", _one(r.FINANCE_COSTS)),)),
    LinePair("pbt", (Sum("profit_before_tax", _one(r.PROFIT_BEFORE_TAX)),)),
    LinePair("net_profit", (
        Sum("profit_loss_for_period (incl. NCI)", _one(r.PROFIT)),
        Sum("profit attributable to owners", _one("profit_or_loss_attributable_to_owners_of_parent")),
    )),  # fmt: skip
    LinePair("equity", (
        Sum("equity (incl. NCI)", _one(r.EQUITY)),
        Sum("equity attributable to owners", _one("equity_attributable_to_owners_of_parent")),
    )),  # fmt: skip
    _borrowings("borrowings_current + borrowings_noncurrent", r.BORROWINGS_CURRENT, r.BORROWINGS_NONCURRENT),
    LinePair("cash_bank", (Sum("cash + other bank balances", _optional(r.CASH, r.OTHER_BANK_BALANCES)),)),
    LinePair("cwip", (Sum("capital_work_in_progress", _optional("capital_work_in_progress")),)),
)

NBFC_PAIRS = tuple(
    _borrowings("debt_securities + borrowings + deposits + subordinated_liabilities",
                "debt_securities", "borrowings", "deposits", "subordinated_liabilities")  # fmt: skip
    if p.line == "borrowings" else p
    for p in IND_AS_PAIRS
)

BANK_PAIRS = (
    LinePair("sales", (Sum("interest_earned", _one("interest_earned")),)),
    LinePair("interest", (Sum("interest_expended", _one("interest_expended")),)),
    LinePair("pbt", (Sum("profit_loss_from_ordinary_activities_before_tax",
                         _one("profit_loss_from_ordinary_activities_before_tax")),)),  # fmt: skip
    LinePair("net_profit", (
        Sum("profit_loss_for_the_period", _one("profit_loss_for_the_period")),
        Sum("profit after minority interest and associates",
            _one("profit_loss_after_taxes_minority_interest_and_share_of_profit_loss_of_associates")),
    )),  # fmt: skip
    LinePair("equity", (Sum("capital + reserves_and_surplus", (Term("capital"), Term("reserves_and_surplus"))),)),
    LinePair("borrowings", (Sum("deposits + borrowings", _optional("deposits", "borrowings")),)),
)

INSURER_PAIRS = (
    LinePair("sales", (
        Sum("net_premium_income", _one("net_premium_income")),
        Sum("gross_premium_income", _one("gross_premium_income")),
        Sum("income", _one("income")),
    ), gate=False, note=_INSURER_NOTE),  # fmt: skip
    LinePair("pbt", (
        Sum("profit_or_loss_before_tax", _one("profit_or_loss_before_tax")),
        Sum("profit_loss_before_tax", _one("profit_loss_before_tax")),
    ), gate=False, note=_INSURER_NOTE),  # fmt: skip
    LinePair("net_profit", (
        Sum("profit_loss_after_tax", _one("profit_loss_after_tax")),
        Sum("profit_loss_after_tax_and_extraordinary_items", _one("profit_loss_after_tax_and_extraordinary_items")),
    ), gate=False, note=_INSURER_NOTE),  # fmt: skip
    LinePair("equity", (
        Sum("share_capital + reserves_and_surplus", (Term("share_capital"), Term("reserves_and_surplus"))),
        Sum("paid_up_equity_share_capital + reserves_and_surplus",
            (Term("paid_up_equity_share_capital"), Term("reserves_and_surplus"))),
    ), gate=False, note=_INSURER_NOTE),  # fmt: skip
    LinePair("borrowings", (Sum("borrowings", _optional("borrowings")),), gate=False, note=_INSURER_NOTE),
)

# Keys are core.db.models.XbrlTaxonomy values; this module imports no models.
PAIRS: Mapping[str, tuple[LinePair, ...]] = {
    "ind_as": IND_AS_PAIRS,
    "nbfc": NBFC_PAIRS,
    "bank": BANK_PAIRS,
    "life_insurance": INSURER_PAIRS,
    "general_insurance": INSURER_PAIRS,
}

ScreenerKey = tuple[str, str, date]  # (statement, line, period_end)
OursKey = tuple[str, date]  # (line_item, period_end): fiscal-year durations and year-end balances

# Screener's Data Sheet lines per statement (ingest/screener_export/parser.py;
# a test checks the two agree).
PL_LINES = (
    "sales", "raw_material", "inventory_change", "power_fuel", "other_mfr", "employee", "selling_admin",
    "other_exp", "other_income", "depreciation", "interest", "pbt", "tax", "net_profit", "dividend",
)  # fmt: skip
BS_LINES = (
    "share_capital", "reserves", "borrowings", "other_liabilities", "total_liabilities", "net_block", "cwip",
    "investments", "other_assets", "total_assets", "receivables", "inventory", "cash_bank", "share_count",
    "new_bonus_shares", "face_value",
)  # fmt: skip
EXPENSE_LINES = ("raw_material", "power_fuel", "other_mfr", "employee", "selling_admin", "other_exp")


def screener_year(values: Mapping[ScreenerKey, Decimal], period_end: date) -> dict[str, Decimal]:
    """Screener's P&L and balance-sheet lines for one fiscal year end, blanks as zero, plus derived lines.

    A statement Screener does not report for `period_end` contributes nothing.
    Derived: operating_profit = sales - (expense lines - inventory_change),
    ebit = pbt + interest, equity = share_capital + reserves,
    capital_employed = equity + borrowings.
    """
    out: dict[str, Decimal] = {}
    for statement, lines in (("pl", PL_LINES), ("bs", BS_LINES)):
        if any((statement, line, period_end) in values for line in lines):
            out.update({line: values.get((statement, line, period_end), Decimal(0)) for line in lines})
    if "sales" in out:
        expenses = sum((out[line] for line in EXPENSE_LINES), Decimal(0)) - out["inventory_change"]
        out["operating_profit"] = out["sales"] - expenses
        out["ebit"] = out["pbt"] + out["interest"]
    if "share_capital" in out:
        out["equity"] = out["share_capital"] + out["reserves"]
        out["capital_employed"] = out["equity"] + out["borrowings"]
    return out


def evaluate_sum(s: Sum, ours: Mapping[OursKey, Decimal], period_end: date) -> tuple[Decimal | None, tuple[str, ...]]:
    """The sum's value and its absent terms. None if a required term is absent, or every term is."""
    absent = tuple(t.item for t in s.terms if (t.item, period_end) not in ours)
    if len(absent) == len(s.terms) or any(not t.optional for t in s.terms if t.item in absent):
        return None, absent
    value = sum((t.sign * ours[t.item, period_end] for t in s.terms if t.item not in absent), Decimal(0))
    return value, absent


def line_tolerance(screener: Decimal) -> Decimal:
    return max(abs(screener) * LINE_RELATIVE, LINE_ABSOLUTE)


def within(ours: Decimal, screener: Decimal, tolerance: Decimal) -> bool:
    return abs(ours - screener) <= tolerance


def compare_line(
    pair: LinePair, screener: Mapping[str, Decimal], ours: Mapping[OursKey, Decimal], period_end: date
) -> Comparison:
    theirs = screener.get(pair.line)
    tolerance = line_tolerance(theirs) if theirs is not None else LINE_ABSOLUTE
    sides = []
    for s in pair.ours:
        value, absent = evaluate_sum(s, ours, period_end)
        if value is None and len(absent) == len(s.terms) and theirs == 0:
            value = Decimal(0)  # absent from the filing, nil on Screener
        ok = value is not None and theirs is not None and within(value, theirs, tolerance)
        sides.append(SideResult(s.label, value, ok, absent))
    primary = sides[0]
    if theirs is None:
        status = Status.MISSING_SCREENER
    elif primary.within:
        status = Status.MATCH
    elif any(side.within for side in sides[1:]):
        status = Status.MATCH_ALTERNATE
    elif primary.value is None:
        status = Status.MISSING_OURS
    else:
        status = Status.DIVERGE
    return Comparison(
        "line", pair.line, period_end, status, theirs, primary.value, tolerance, pair.gate, tuple(sides),
        ((pair.line, theirs),), pair.note,
    )  # fmt: skip


def compare_lines(
    family: str, screener_values: Mapping[ScreenerKey, Decimal], ours: Mapping[OursKey, Decimal], period_end: date
) -> list[Comparison]:
    """Every line pair of `family` for the fiscal year ending `period_end`."""
    screener = screener_year(screener_values, period_end)
    return [compare_line(pair, screener, ours, period_end) for pair in PAIRS[family]]


# --------------------------------------------------------------------------- #
# Ratios (ratios/1 families only)
# --------------------------------------------------------------------------- #

ROCE_CLOSING = "roce_closing_capital"  # diagnostic for the average-or-closing question; not a ratios/1 ratio


@dataclass(frozen=True)
class RatioSpec:
    ratio: str  # a ratios.Ratio value, or ROCE_CLOSING
    tolerance: Decimal | None  # None: the relative coverage tolerance
    inputs: tuple[str, ...]  # Screener lines shown beside the ratio
    gate: bool = True
    note: str = ""


RATIO_SPECS = (
    RatioSpec(r.Ratio.OPERATING_MARGIN, MARGIN_TOLERANCE, ("operating_profit", "sales")),
    RatioSpec(r.Ratio.EBIT_MARGIN, MARGIN_TOLERANCE, ("pbt", "interest", "sales")),
    RatioSpec(r.Ratio.NET_MARGIN, MARGIN_TOLERANCE, ("net_profit", "sales")),
    RatioSpec(r.Ratio.INTEREST_COVERAGE, None, ("pbt", "interest")),
    RatioSpec(r.Ratio.DEBT_TO_EQUITY, LEVERAGE_TOLERANCE, ("borrowings", "equity")),
    RatioSpec(r.Ratio.NET_DEBT_TO_EQUITY, LEVERAGE_TOLERANCE, ("borrowings", "cash_bank", "equity"), gate=False,
              note="Screener has no current-investments line: its net debt subtracts cash & bank only"),  # fmt: skip
    RatioSpec(r.Ratio.ROCE, MARGIN_TOLERANCE, ("ebit", "capital_employed")),
    RatioSpec(r.Ratio.INCREMENTAL_ROCE, MARGIN_TOLERANCE, ("ebit", "capital_employed")),
    RatioSpec(ROCE_CLOSING, MARGIN_TOLERANCE, ("ebit", "capital_employed"), gate=False,
              note="diagnostic: EBIT over closing capital employed"),  # fmt: skip
)


def _safe(formula: Callable[[], Decimal | None]) -> Decimal | None:
    """None when an input is missing or invalid (e.g. negative finance costs)."""
    try:
        return formula()
    except (KeyError, ValueError):
        return None


def screener_ratio(ratio: str, years: Mapping[date, Mapping[str, Decimal]], period_end: date) -> Decimal | None:
    """One ratio computed from Screener's lines with the ratios/1 functions. None if missing or undefined."""
    y = years.get(period_end, {})
    prior = years.get(r.shift_years(period_end, -1), {})
    base = years.get(r.shift_years(period_end, -r.INCREMENTAL_ROCE_YEARS), {})
    formulas: dict[str, Callable[[], Decimal | None]] = {
        r.Ratio.OPERATING_MARGIN: lambda: r.margin(y["operating_profit"], y["sales"]),
        r.Ratio.EBIT_MARGIN: lambda: r.margin(r.ebit(y["pbt"], y["interest"]), y["sales"]),
        r.Ratio.NET_MARGIN: lambda: r.margin(y["net_profit"], y["sales"]),
        r.Ratio.INTEREST_COVERAGE: lambda: r.interest_coverage(r.ebit(y["pbt"], y["interest"]), y["interest"]),
        r.Ratio.DEBT_TO_EQUITY: lambda: r.debt_to_equity(y["borrowings"], y["equity"]),
        r.Ratio.NET_DEBT_TO_EQUITY: lambda: r.debt_to_equity(
            r.net_debt(y["borrowings"], y["cash_bank"], Decimal(0), Decimal(0)), y["equity"]
        ),
        r.Ratio.ROCE: lambda: r.roce(y["ebit"], r.average_balance(prior["capital_employed"], y["capital_employed"])),
        r.Ratio.INCREMENTAL_ROCE: lambda: r.incremental_roce(
            y["ebit"], base["ebit"], y["capital_employed"], base["capital_employed"]
        ),
        ROCE_CLOSING: lambda: r.roce(y["ebit"], y["capital_employed"]),
    }
    return _safe(formulas[ratio])


def ours_closing_roce(ours: Mapping[OursKey, Decimal], period_end: date) -> tuple[Decimal | None, str | None]:
    """Our EBIT over our closing capital employed, as (value, gap) like core.db.pit.RatioValue."""
    ebit_value, _ = evaluate_sum(Sum("ebit", EBIT_TERMS), ours, period_end)
    capital, _ = evaluate_sum(Sum("capital", CAPITAL_TERMS), ours, period_end)
    if ebit_value is None or capital is None:
        return None, "missing_input"
    value = _safe(lambda: r.roce(ebit_value, capital))
    return value, None if value is not None else "undefined"


def _ratio_status(theirs: Decimal | None, value: Decimal | None, gap: str | None, tolerance: Decimal,
                  inputs: tuple[tuple[str, Decimal | None], ...]) -> Status:  # fmt: skip
    if value is not None and theirs is not None:
        return Status.MATCH if within(value, theirs, tolerance) else Status.DIVERGE
    if theirs is None and any(v is None for _, v in inputs):
        return Status.MISSING_SCREENER
    if value is None and gap != "undefined":
        return Status.MISSING_OURS
    if value is None and theirs is None:
        return Status.MATCH  # undefined on both sides, e.g. no finance costs
    return Status.DIVERGE  # defined on one side only


def compare_ratios(
    screener_values: Mapping[ScreenerKey, Decimal],
    ours_ratios: Mapping[tuple[str, date], tuple[Decimal | None, str | None]],
    ours: Mapping[OursKey, Decimal],
    period_end: date,
) -> list[Comparison]:
    """ratios/1 ratios for one fiscal year: Screener's lines through our formulas, against ours read at `t`.

    `ours_ratios` maps (ratio, period_end) to (value, gap value) from
    core.db.pit.ratios_as_of, fiscal-year rows only. A ratio missing from it
    is a missing input.
    """
    years = {d: screener_year(screener_values, d) for d in {k[2] for k in screener_values}}
    y = years.get(period_end, {})
    prior = years.get(r.shift_years(period_end, -1), {})
    base = years.get(r.shift_years(period_end, -r.INCREMENTAL_ROCE_YEARS), {})
    out = []
    for spec in RATIO_SPECS:
        theirs = screener_ratio(spec.ratio, years, period_end)
        if spec.ratio == ROCE_CLOSING:
            value, gap = ours_closing_roce(ours, period_end)
        else:
            value, gap = ours_ratios.get((spec.ratio, period_end), (None, "missing_input"))
        inputs = tuple((line, y.get(line)) for line in spec.inputs)
        if spec.ratio == r.Ratio.ROCE:
            inputs += (("capital_employed, prior year", prior.get("capital_employed")),)
        if spec.ratio == r.Ratio.INCREMENTAL_ROCE:
            inputs += (("ebit, base year", base.get("ebit")), ("capital_employed, base year", base.get("capital_employed")))
        tolerance = spec.tolerance
        if tolerance is None:
            tolerance = max(abs(theirs or Decimal(0)) * COVERAGE_RELATIVE, COVERAGE_ABSOLUTE)
        status = _ratio_status(theirs, value, gap, tolerance, inputs)
        out.append(Comparison("ratio", str(spec.ratio), period_end, status, theirs, value, tolerance, spec.gate,
                              (), inputs, spec.note))  # fmt: skip
    return out


def gate_passes(comparisons: list[Comparison]) -> bool:
    """The Week 2 gate for one company: at least one gated comparison, and none failing."""
    gated = [c for c in comparisons if c.gate]
    return bool(gated) and not any(c.status in FAILING for c in gated)
