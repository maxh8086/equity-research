"""Guidance claims graded against what was reported, and what management stopped saying.

Pure (R1): callers pass in the claims and the facts already read at `t`
(core.db.pit); nothing here reads a store, a clock or a model. Nothing is
stored either (R2): resolution and silence are computed at read time from
rows with as_of <= t, so the same claim is OPEN today and MET after the
results are public, and a restatement that lands later changes the grade of a
read made after it and no read made before it.

Grading is code because "did they deliver" is the number the credibility
ledger is built on. A claim is compared with the reported figure by a fixed
rule under RULE_VERSION, never by a judgement that could drift.

A claim that cannot be graded is not guessed at (R3). It comes back
UNRESOLVABLE with the reason: no number in it, a period nobody can date, a
metric the filings do not carry, a unit that is not the actual's unit, an
input missing, or an answer that was already public on the day it was made.
Only MET and MISSED enter the delivery rate.

Percent values are percent points (15 means 15%), as parse_value stores "15%".
Facts are absolute rupees; claims are in crore, so an actual is shown in crore.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal

from core.compute import ratios
from core.compute.guidance import (
    HEDGE_RANK,
    ComparableClaim,
    HedgeStrength,
    ParsedValue,
    Specificity,
    Unit,
)

# Bump when a definition, a tolerance or the grading changes. A change is a new
# version, never an edit of what an old one meant.
RULE_VERSION = "guidance-resolution-2026-09-29"


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #


class PeriodKind(StrEnum):
    FY = "FY"
    H1 = "H1"
    H2 = "H2"
    Q1 = "Q1"
    Q2 = "Q2"
    Q3 = "Q3"
    Q4 = "Q4"


@dataclass(frozen=True)
class FiscalPeriod:
    kind: PeriodKind
    period_start: date
    period_end: date


# Indian fiscal year: 1 April to 31 March, named for the year it ends in (FY27
# ends 31 March 2027). Each kind's span as (year offset from the FY's end year,
# month, day) for its first and last day.
_SPANS: dict[PeriodKind, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    PeriodKind.FY: ((-1, 4, 1), (0, 3, 31)),
    PeriodKind.H1: ((-1, 4, 1), (-1, 9, 30)),
    PeriodKind.H2: ((-1, 10, 1), (0, 3, 31)),
    PeriodKind.Q1: ((-1, 4, 1), (-1, 6, 30)),
    PeriodKind.Q2: ((-1, 7, 1), (-1, 9, 30)),
    PeriodKind.Q3: ((-1, 10, 1), (-1, 12, 31)),
    PeriodKind.Q4: ((0, 1, 1), (0, 3, 31)),
}

# Years outside this are typos or dates, not fiscal years anyone guided about.
_MIN_FISCAL_YEAR, _MAX_FISCAL_YEAR = 1990, 2100

_PERIOD = re.compile(
    r"(?:(?P<part>[qh])\s*(?P<n>\d)\s*)?"
    r"(?:fy|fiscal(?:\s+year)?)\s*"
    r"(?P<a>\d{4}|\d{2})(?:\s*-\s*(?P<b>\d{4}|\d{2}))?",
    re.ASCII,
)


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def parse_period_label(label: str) -> FiscalPeriod | None:
    """The fiscal period a label names, or None when it names none.

    "FY27", "FY2027", "FY 2026-27", "FY26-27", "fiscal 2027", "Q3 FY26",
    "Q3FY26", "H1 FY27", in any case and spacing. A two-digit year Y is 2000+Y.
    "next 3 years", "medium term", "by 2030" and "this year" are None: they
    are not a period the filings can be asked about. Total: never raises.
    """
    match = _PERIOD.fullmatch(_normalise(label))
    if match is None:
        return None
    first = _year(match["a"], century_of=None)
    if match["b"] is None:
        end_year = first
    else:
        second = _year(match["b"], century_of=first)
        if second != first + 1:
            return None
        end_year = second
    if not _MIN_FISCAL_YEAR <= end_year <= _MAX_FISCAL_YEAR:
        return None
    if match["part"] is None:
        kind = PeriodKind.FY
    else:
        kind = _part_kind(match["part"], int(match["n"]))
        if kind is None:
            return None
    (s_off, s_month, s_day), (e_off, e_month, e_day) = _SPANS[kind]
    return FiscalPeriod(
        kind,
        date(end_year + s_off, s_month, s_day),
        date(end_year + e_off, e_month, e_day),
    )


def _year(digits: str, *, century_of: int | None) -> int:
    if len(digits) == 4:
        return int(digits)
    if century_of is None:
        return 2000 + int(digits)
    return century_of // 100 * 100 + int(digits)


def _part_kind(part: str, n: int) -> PeriodKind | None:
    if part == "q" and 1 <= n <= 4:
        return PeriodKind(f"Q{n}")
    if part == "h" and 1 <= n <= 2:
        return PeriodKind(f"H{n}")
    return None


# What identifies "the same period" across spellings: the parsed (kind, end
# date), else the label itself, lower-cased and whitespace-collapsed.
PeriodKey = tuple[PeriodKind, date] | str


def period_key(label: str) -> PeriodKey:
    period = parse_period_label(label)
    if period is None:
        return _normalise(label)
    return (period.kind, period.period_end)


def claim_key(metric: str, period_label: str) -> tuple[str, PeriodKey]:
    return (metric, period_key(period_label))


# --------------------------------------------------------------------------- #
# Statuses, the rule, and grading
# --------------------------------------------------------------------------- #


class Status(StrEnum):
    MET = "met"
    MISSED = "missed"
    OPEN = "open"  # the target period's facts are not public at t
    UNRESOLVABLE = "unresolvable"  # see UnresolvableReason


class UnresolvableReason(StrEnum):
    DIRECTIONAL = "directional"  # no number to grade
    PERIOD_UNPARSED = "period_unparsed"  # the label names no fiscal period
    METRIC_NOT_IN_XBRL = "metric_not_in_xbrl"  # a data gap: the filings do not carry it
    UNIT_MISMATCH = "unit_mismatch"  # not the actual's unit; never converted
    INPUT_MISSING = "input_missing"  # a fact is absent, or the arithmetic is undefined
    ALREADY_KNOWN_WHEN_MADE = "already_known_when_made"  # public at the claim's own as_of
    PERIOD_NOT_SUPPORTED = "period_not_supported"  # the metric is defined for other periods only


class Basis(StrEnum):
    """Which filing a fact comes from; the values are the store's consolidation values."""

    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"


@dataclass(frozen=True)
class ResolutionRule:
    """The parameters of grading and of silence.

    Both are UNVALIDATED placeholders. CLAUDE.md: parameters are chosen on one
    period and measured on a later one; neither has been. They live here, in
    one frozen object under RULE_VERSION, so a change is a new version and
    every result can say which it was graded under.
    """

    # A point target ("15% margin") is met within this fraction of itself.
    # 5% is a placeholder.
    point_tolerance: Decimal = Decimal("0.05")
    # Consecutive later calls that must all be clean and silent on a claim.
    # 2 is a placeholder.
    consecutive_calls: int = 2

    def __post_init__(self) -> None:
        if self.point_tolerance < 0:
            raise ValueError(f"point_tolerance must not be negative, got {self.point_tolerance}")
        if self.consecutive_calls < 1:
            raise ValueError(f"consecutive_calls must be at least 1, got {self.consecutive_calls}")


DEFAULT_RULE = ResolutionRule()


def _defect(value: ParsedValue, unit: Unit) -> UnresolvableReason | None:
    """Why a claim cannot be compared with an actual in `unit`, or None if it can."""
    if value.specificity is Specificity.DIRECTIONAL:
        return UnresolvableReason.DIRECTIONAL
    if value.unit is not unit:
        return UnresolvableReason.UNIT_MISMATCH
    low, high = value.low, value.high
    if value.specificity is Specificity.POINT:
        well_formed = low is not None and low == high
    elif value.specificity is Specificity.RANGE:
        well_formed = low is not None and high is not None and low <= high
    else:  # BOUND: exactly one side
        well_formed = (low is None) != (high is None)
    return None if well_formed else UnresolvableReason.INPUT_MISSING


def grade(
    value: ParsedValue, actual: Decimal, unit: Unit, rule: ResolutionRule = DEFAULT_RULE
) -> tuple[Status, UnresolvableReason | None]:
    """MET or MISSED for `actual` (in `unit`) against a claim, else UNRESOLVABLE and why.

    A range is met between its ends, inclusive. A floor is met at or above it,
    a ceiling at or below, both exact. A point is met within
    rule.point_tolerance of itself, relative to its magnitude; a point of zero
    needs an exact match. A claim in another unit is not converted: a
    50 bps expansion is a change, and the actual here is a level.
    """
    defect = _defect(value, unit)
    if defect is not None:
        return Status.UNRESOLVABLE, defect
    low, high = value.low, value.high
    if value.specificity is Specificity.POINT:
        assert low is not None
        met = abs(actual - low) <= rule.point_tolerance * abs(low)
    else:
        met = (low is None or actual >= low) and (high is None or actual <= high)
    return (Status.MET if met else Status.MISSED), None


# --------------------------------------------------------------------------- #
# Facts and the metrics they answer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Fact:
    """One reported value, as the resolver needs it.

    `period_start` is None for a balance-sheet (instant) fact. `value` is
    absolute rupees. It carries no as_of: the caller hands over the version
    that was current at t (core.db.pit.facts_as_of), one per key.
    """

    line_item: str
    period_start: date | None
    period_end: date
    value: Decimal
    basis: Basis


class Anchor(StrEnum):
    """Where a needed fact sits relative to the claim's period."""

    PERIOD = "period"  # a duration fact over the period itself
    PERIOD_END = "period_end"  # a balance on the period's last day
    PRIOR_YEAR = "prior_year"  # a duration fact over the same period a year earlier
    OPENING = "opening"  # a balance on the day before the period starts


_TARGET_ANCHORS = frozenset({Anchor.PERIOD, Anchor.PERIOD_END})


@dataclass(frozen=True)
class Need:
    line_item: str
    anchor: Anchor


Inputs = Mapping[Need, Decimal]


@dataclass(frozen=True)
class MetricResolver:
    """How one metric's actual is computed from facts.

    `compute` gets the value of every need, keyed by need, and returns the
    actual in `unit`, or None where the arithmetic is undefined (a margin on
    zero revenue). Facts of the target period decide OPEN: the answer is not
    public until at least one of them is.
    """

    unit: Unit
    needs: tuple[Need, ...]
    compute: Callable[[Inputs], Decimal | None]
    fiscal_year_only: bool = False


def _crore(rupees: Decimal) -> Decimal:
    return rupees.scaleb(-7)  # 1 crore = 10^7 rupees; exact


def _percent(fraction: Decimal | None) -> Decimal | None:
    return None if fraction is None else fraction.scaleb(2)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if not denominator > 0:
        return None
    with localcontext(prec=ratios.PRECISION):
        return numerator / denominator


REV = Need(ratios.REVENUE, Anchor.PERIOD)
REV_PRIOR = Need(ratios.REVENUE, Anchor.PRIOR_YEAR)
PAT = Need(ratios.PROFIT, Anchor.PERIOD)
EXPENSES = Need(ratios.TOTAL_EXPENSES, Anchor.PERIOD)
FINANCE = Need(ratios.FINANCE_COSTS, Anchor.PERIOD)
DEPRECIATION = Need(ratios.DEPRECIATION, Anchor.PERIOD)
PBT = Need(ratios.PROFIT_BEFORE_TAX, Anchor.PERIOD)
CAPEX_PPE = Need("purchase_of_property_plant_and_equipment_classified_as_investing_activities", Anchor.PERIOD)
_BALANCE_ITEMS = (
    ratios.BORROWINGS_CURRENT,
    ratios.BORROWINGS_NONCURRENT,
    ratios.CASH,
    ratios.OTHER_BANK_BALANCES,
    ratios.CURRENT_INVESTMENTS,
)
_CAPITAL_ITEMS = (ratios.EQUITY, ratios.BORROWINGS_CURRENT, ratios.BORROWINGS_NONCURRENT)


def _revenue(v: Inputs) -> Decimal | None:
    return _crore(v[REV])


def _revenue_growth(v: Inputs) -> Decimal | None:
    # Growth on a base that is not positive is undefined, not zero.
    return _percent(_ratio(v[REV] - v[REV_PRIOR], v[REV_PRIOR]))


def _pat(v: Inputs) -> Decimal | None:
    return _crore(v[PAT])


def _pat_margin(v: Inputs) -> Decimal | None:
    return _percent(ratios.margin(v[PAT], v[REV]))


def _ebitda_rupees(v: Inputs) -> Decimal:
    return ratios.operating_profit(v[REV], v[EXPENSES], v[FINANCE], v[DEPRECIATION])


def _ebitda(v: Inputs) -> Decimal | None:
    return _crore(_ebitda_rupees(v))


def _ebitda_margin(v: Inputs) -> Decimal | None:
    return _percent(ratios.margin(_ebitda_rupees(v), v[REV]))


def _net_debt(v: Inputs) -> Decimal | None:
    def at_end(item: str) -> Decimal:
        return v[Need(item, Anchor.PERIOD_END)]

    borrowings = ratios.total_borrowings(at_end(ratios.BORROWINGS_CURRENT), at_end(ratios.BORROWINGS_NONCURRENT))
    net = ratios.net_debt(
        borrowings, at_end(ratios.CASH), at_end(ratios.OTHER_BANK_BALANCES), at_end(ratios.CURRENT_INVESTMENTS)
    )
    return _crore(net)


def _capex(v: Inputs) -> Decimal | None:
    # Reported as a positive outflow. A negative figure is not repaired.
    return None if v[CAPEX_PPE] < 0 else _crore(v[CAPEX_PPE])


def _capital_employed(v: Inputs, anchor: Anchor) -> Decimal:
    borrowings = ratios.total_borrowings(
        v[Need(ratios.BORROWINGS_CURRENT, anchor)], v[Need(ratios.BORROWINGS_NONCURRENT, anchor)]
    )
    return ratios.capital_employed(v[Need(ratios.EQUITY, anchor)], borrowings)


def _roce(v: Inputs) -> Decimal | None:
    average = ratios.average_balance(
        _capital_employed(v, Anchor.OPENING), _capital_employed(v, Anchor.PERIOD_END)
    )
    return _percent(ratios.roce(ratios.ebit(v[PBT], v[FINANCE]), average))


def _at_end(items: Iterable[str]) -> tuple[Need, ...]:
    return tuple(Need(item, Anchor.PERIOD_END) for item in items)


def _at_opening(items: Iterable[str]) -> tuple[Need, ...]:
    return tuple(Need(item, Anchor.OPENING) for item in items)


# Every metric the filings can answer, and how. Hardcoded, like the news alias
# table: a metric not here is a data gap (METRIC_NOT_IN_XBRL), never an
# approximation, and adding one is a reviewed change under a new RULE_VERSION.
# Keyed on the metric's string value: core/ does not import extract/.
#
# Definitions, each the one ratios/1 already fixes where it has one:
#   revenue, pat        revenue from operations; profit for the period (before
#                       non-controlling interests), in crore
#   revenue_growth      revenue over the same period a year earlier, less one
#   pat_margin          profit over revenue
#   ebitda[_margin]     revenue less expenses other than finance costs and
#                       depreciation (ratios.operating_profit); other income
#                       is excluded
#   net_debt            borrowings less cash, other bank balances and current
#                       investments (ratios.net_debt) on the period's last day
#   capex               purchase of property, plant and equipment ONLY.
#                       Intangibles, investment property and other long-term
#                       assets are not counted, so a claim that includes them
#                       will read low and can be graded MISSED
#   roce                EBIT over average capital employed (ratios.roce), for
#                       a full fiscal year only, as ratios/1 defines it
# A metric that is a percent is in percent points, the rest in crore.
METRIC_RESOLVERS: dict[str, MetricResolver] = {
    "revenue": MetricResolver(Unit.INR_CRORE, (REV,), _revenue),
    "revenue_growth": MetricResolver(Unit.PERCENT, (REV, REV_PRIOR), _revenue_growth),
    "pat": MetricResolver(Unit.INR_CRORE, (PAT,), _pat),
    "pat_margin": MetricResolver(Unit.PERCENT, (PAT, REV), _pat_margin),
    "ebitda": MetricResolver(Unit.INR_CRORE, (REV, EXPENSES, FINANCE, DEPRECIATION), _ebitda),
    "ebitda_margin": MetricResolver(
        Unit.PERCENT, (REV, EXPENSES, FINANCE, DEPRECIATION), _ebitda_margin
    ),
    "net_debt": MetricResolver(Unit.INR_CRORE, _at_end(_BALANCE_ITEMS), _net_debt),
    "capex": MetricResolver(Unit.INR_CRORE, (CAPEX_PPE,), _capex),
    "roce": MetricResolver(
        Unit.PERCENT,
        (PBT, FINANCE, *_at_end(_CAPITAL_ITEMS), *_at_opening(_CAPITAL_ITEMS)),
        _roce,
        fiscal_year_only=True,
    ),
}


# --------------------------------------------------------------------------- #
# Resolving one claim
# --------------------------------------------------------------------------- #

# (basis, line_item, period_start, period_end) -> value
_FactKey = tuple[Basis, str, date | None, date]


@dataclass(frozen=True)
class Resolution:
    """What became of one claim, and everything that decided it.

    `inputs` are the (line_item, period_end, value) facts the actual was built
    from, so every number traces to a fact. `basis` is None until any fact of
    the period is public. `actual` is set where one was computed, including
    ALREADY_KNOWN_WHEN_MADE, where it is the figure that was public then.
    """

    status: Status
    reason: UnresolvableReason | None
    actual: Decimal | None
    unit: Unit | None
    basis: Basis | None
    period: FiscalPeriod | None
    inputs: tuple[tuple[str, date, Decimal], ...] = ()
    rule_version: str = RULE_VERSION


def _index(facts: Iterable[Fact]) -> dict[_FactKey, Decimal]:
    """Facts by key. Two different values for one key is the caller's bug, not a choice for us."""
    indexed: dict[_FactKey, Decimal] = {}
    for fact in facts:
        key = (fact.basis, fact.line_item, fact.period_start, fact.period_end)
        if key in indexed and indexed[key] != fact.value:
            raise ValueError(f"conflicting values for {key}: {indexed[key]} and {fact.value}")
        indexed[key] = fact.value
    return indexed


def _key(need: Need, period: FiscalPeriod, basis: Basis) -> _FactKey:
    start, end = period.period_start, period.period_end
    if need.anchor is Anchor.PERIOD:
        return (basis, need.line_item, start, end)
    if need.anchor is Anchor.PERIOD_END:
        return (basis, need.line_item, None, end)
    if need.anchor is Anchor.PRIOR_YEAR:
        return (basis, need.line_item, ratios.shift_years(start, -1), ratios.shift_years(end, -1))
    return (basis, need.line_item, None, ratios.prior_balance_date(start))


@dataclass(frozen=True)
class _Computed:
    state: Literal["open", "missing", "ok"]
    basis: Basis | None = None
    actual: Decimal | None = None
    inputs: tuple[tuple[str, date, Decimal], ...] = ()


def _compute(resolver: MetricResolver, period: FiscalPeriod, index: Mapping[_FactKey, Decimal]) -> _Computed:
    # One basis per actual, never a blend: consolidated when any of the
    # period's own facts is consolidated, else standalone.
    target = [n for n in resolver.needs if n.anchor in _TARGET_ANCHORS]
    for basis in Basis:
        if any(_key(n, period, basis) in index for n in target):
            break
    else:
        return _Computed("open")
    keys = {n: _key(n, period, basis) for n in resolver.needs}
    if any(key not in index for key in keys.values()):
        return _Computed("missing", basis)
    values = {n: index[key] for n, key in keys.items()}
    try:
        actual = resolver.compute(values)
    except ValueError:  # ratios refuses a negative balance it cannot interpret
        return _Computed("missing", basis)
    if actual is None:
        return _Computed("missing", basis)
    inputs = tuple((n.line_item, keys[n][3], values[n]) for n in resolver.needs)
    return _Computed("ok", basis, actual, inputs)


def resolve_claim(
    claim: ComparableClaim,
    facts: Iterable[Fact],
    *,
    facts_when_made: Iterable[Fact] | None = None,
    rule: ResolutionRule = DEFAULT_RULE,
) -> Resolution:
    """Grade one claim against the facts public at t.

    `facts` are those read at t (one version per key). `facts_when_made`, when
    given, are those read at the claim's own as_of: if the actual could already
    be computed from them, the claim was not a forecast and is UNRESOLVABLE
    (ALREADY_KNOWN_WHEN_MADE) whatever it says. A restatement after the claim
    does not change that: the question is whether an answer existed, not
    whether it was the same one.

    OPEN means none of the period's own facts is public yet. Once any is, a
    missing or undefined input is INPUT_MISSING, not OPEN: waiting will not
    help a comparison the filings never carried. Ungradable claims are settled
    first, from the claim alone, in the order of UnresolvableReason.
    """
    period = parse_period_label(claim.period_label)
    resolver = METRIC_RESOLVERS.get(claim.metric)
    unit = None if resolver is None else resolver.unit

    def result(
        status: Status,
        reason: UnresolvableReason | None = None,
        *,
        computed: _Computed | None = None,
    ) -> Resolution:
        c = computed or _Computed("open")
        return Resolution(status, reason, c.actual, unit, c.basis, period, c.inputs)

    value = claim.value
    if value.specificity is Specificity.DIRECTIONAL:
        return result(Status.UNRESOLVABLE, UnresolvableReason.DIRECTIONAL)
    if period is None:
        return result(Status.UNRESOLVABLE, UnresolvableReason.PERIOD_UNPARSED)
    if resolver is None:
        return result(Status.UNRESOLVABLE, UnresolvableReason.METRIC_NOT_IN_XBRL)
    defect = _defect(value, resolver.unit)
    if defect is not None:
        return result(Status.UNRESOLVABLE, defect)
    if resolver.fiscal_year_only and not ratios.is_fiscal_year(period.period_start, period.period_end):
        return result(Status.UNRESOLVABLE, UnresolvableReason.PERIOD_NOT_SUPPORTED)

    if facts_when_made is not None:
        then = _compute(resolver, period, _index(facts_when_made))
        if then.state == "ok":
            return result(Status.UNRESOLVABLE, UnresolvableReason.ALREADY_KNOWN_WHEN_MADE, computed=then)
    now = _compute(resolver, period, _index(facts))
    if now.state == "open":
        return result(Status.OPEN)
    if now.state == "missing" or now.actual is None:
        return result(Status.UNRESOLVABLE, UnresolvableReason.INPUT_MISSING, computed=now)
    status, reason = grade(value, now.actual, resolver.unit, rule)
    return result(status, reason, computed=now)


def period_resolved(resolution: Resolution) -> bool:
    """Whether the claim's target period is settled, so that silence can no longer be silence.

    A graded claim is; so is one whose answer was public when it was made.
    OPEN is not, and neither is a claim that can never be graded, since no
    answer will ever arrive to settle it.
    """
    return resolution.status in (Status.MET, Status.MISSED) or (
        resolution.reason is UnresolvableReason.ALREADY_KNOWN_WHEN_MADE
    )


# --------------------------------------------------------------------------- #
# Delivery rate
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ClaimOutcome:
    """One claim's status and hedge, as the rate needs them.

    `silent` is set by the caller from detect_silent. Only a claim that is
    not graded can be silent: a graded claim has an answer, whatever was said.
    """

    status: Status
    hedge: HedgeStrength
    silent: bool = False


@dataclass(frozen=True)
class DeliveryRate:
    rate: Decimal | None  # None when nothing is graded
    met: int
    missed: int
    open: int
    unresolvable: int
    silent: int


def delivery_rate(
    outcomes: Iterable[ClaimOutcome], *, ranks: Mapping[HedgeStrength, int | Decimal] = HEDGE_RANK
) -> DeliveryRate:
    """The hedge-weighted share of graded claims that were met.

    sum(rank of MET) / sum(rank of MET and MISSED), ranks from HEDGE_RANK: a
    kept "will" counts for more than a kept "working towards", and a missed
    "will" costs more. Only MET and MISSED are in the rate. OPEN,
    UNRESOLVABLE and SILENT are counted apart and are not misses: whether
    silence should count against management is an open research question, not
    settled here. None, never zero, when nothing is graded.
    """
    met = missed = open_ = unresolvable = silent = 0
    met_weight = missed_weight = Decimal(0)
    for outcome in outcomes:
        graded = outcome.status in (Status.MET, Status.MISSED)
        if outcome.silent and graded:
            raise ValueError(f"a {outcome.status} claim cannot also be silent")
        if outcome.silent:
            silent += 1
        elif outcome.status is Status.MET:
            met += 1
            met_weight += Decimal(ranks[outcome.hedge])
        elif outcome.status is Status.MISSED:
            missed += 1
            missed_weight += Decimal(ranks[outcome.hedge])
        elif outcome.status is Status.OPEN:
            open_ += 1
        else:
            unresolvable += 1
    rate = _ratio(met_weight, met_weight + missed_weight)
    return DeliveryRate(rate, met, missed, open_, unresolvable, silent)


def select_claims(
    claims: Iterable[ComparableClaim], basis: Literal["first", "last"] = "first"
) -> list[ComparableClaim]:
    """One claim per (metric, period) to feed the delivery rate: the first, or the last.

    The first is the original promise. Grading only the final reiteration lets
    management lower a target mid-year and then "meet" it. Groups keep the
    order in which they first appear. Every claim is still resolvable on its
    own; this only chooses which ones count.
    """
    if basis not in ("first", "last"):
        raise ValueError(f"basis must be 'first' or 'last', got {basis!r}")
    chosen: dict[tuple[str, PeriodKey], ComparableClaim] = {}
    for claim in claims:
        key = claim_key(claim.metric, claim.period_label)
        if basis == "last" or key not in chosen:
            chosen[key] = claim
    return list(chosen.values())


# --------------------------------------------------------------------------- #
# SILENT
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TranscriptMentions:
    """What one later call said about guidance, as silence detection needs it.

    `mentions` are the (metric, period_key) of every claim extracted from the
    call. `clean` is False when any of its claims was quarantined
    (claims_quarantined > 0): the claim in question may be sitting there.
    """

    call_date: date
    mentions: frozenset[tuple[str, PeriodKey]]
    clean: bool = True


def detect_silent(
    metric: str,
    key: PeriodKey,
    later: Sequence[TranscriptMentions],
    *,
    resolved: bool,
    rule: ResolutionRule = DEFAULT_RULE,
) -> bool:
    """Whether management dropped a claim: its period is unsettled and it went unmentioned.

    Silent when the claim's period is not resolved (see period_resolved) and
    each of the next rule.consecutive_calls later calls, in call-date order, is
    clean and does not mention (metric, key). Fewer later calls than that is
    not silent, yet. A call with quarantined claims cannot establish absence,
    so it prevents silence rather than granting it. A call beyond the window
    is not looked at.
    """
    if resolved:
        return False
    window = sorted(later, key=lambda t: t.call_date)[: rule.consecutive_calls]
    if len(window) < rule.consecutive_calls:
        return False
    return all(t.clean and (metric, key) not in t.mentions for t in window)
