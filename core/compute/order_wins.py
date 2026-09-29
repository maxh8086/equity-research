"""Order-win facts from NSE announcements (Session 7e).

Turns an announcement's subject and description into an `OrderWin` fact, or a
`Quarantined` record that says why not. Everything is code (R1): the text
decision is `core.compute.order_terms.classify`, the money figure is parsed by
`order_terms.parse_amounts`, and the counterparty and execution period come
from hardcoded patterns under `RULE_VERSION`. Each extracted field keeps the
verbatim quote it came from, so a reader can check the parse against the
filing. A model never supplies a number here.

A gap is visible, never filled: a missing counterparty or execution period is
named in `OrderWin.missing`; a missing, doubled or conflicting order value
quarantines the row, because two different rupee figures in one filing means
code cannot say which is the order.

`order_intensity` is arithmetic: order value over trailing twelve-month
revenue as a percentage, and the same figure annualised across the stated
execution period. No threshold appears here. Whether a percentage matters is
the caller's decision, and the output carries no action or verdict field.

Pure: callers pass everything in. No DB, no I/O, no LLM, no clock. Money is
`Decimal`. Every input carries `as_of`; inputs dated after the `t` argument
raise `LookAhead` (R2).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from core.compute import order_terms

RULE_VERSION = "order_wins/1"

DAYS_PER_MONTH = Decimal("30.4375")  # 365.25 / 12, exact
_QUARTER_DAYS = range(85, 96)

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
_MONTHS = {name: i for i, name in enumerate(_MONTH_NAMES, start=1)}
_MONTHS.update({name[:3]: i for i, name in enumerate(_MONTH_NAMES, start=1)})
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_COUNTERPARTY_RE = re.compile(
    r"\b(?i:from|by)\s+"
    r"([A-Z][\w&.'\-]*(?:\s+(?:(?:of|and|for|&)\s+)?[A-Z][\w&.'\-]*)*)"
)
_DURATION_RE = re.compile(
    r"\b(?i:within|over|in|period of)\s+(\d+(?:\.\d+)?)\s+(?i:(months?|years?))\b"
)
_END_DMY_RE = re.compile(
    rf"\b(?i:by|before)\s+(\d{{1,2}})[-\s/]({_MONTH_ALT})[-\s/,]+(\d{{4}})\b", re.IGNORECASE
)
_END_MY_RE = re.compile(rf"\b(?i:by|before)\s+({_MONTH_ALT})[\s,]+(\d{{4}})\b", re.IGNORECASE)


class LookAhead(ValueError):
    """Evidence dated after the observation reached the computation. Always a bug."""


class QuarantineReason(StrEnum):
    NOT_AN_ORDER = "not_an_order"
    PRE_AWARD = "pre_award"  # lowest bidder, not yet awarded
    EXCLUDED = "excluded"  # a tax, regulatory or judicial order
    AMBIGUOUS = "ambiguous"  # award and exclusion wording together
    NO_VALUE = "no_value"
    MULTIPLE_VALUES = "multiple_values"


@dataclass(frozen=True)
class Announcement:
    isin: str
    subject: str
    description: str | None
    announced_on: datetime
    as_of: datetime
    source_url: str


@dataclass(frozen=True)
class OrderWin:
    isin: str
    announced_on: datetime
    as_of: datetime
    source_url: str
    order_value_inr: Decimal
    value_quote: str
    counterparty: str | None
    execution_months: Decimal | None
    execution_end: date | None
    period_quote: str | None
    matched_phrase: str
    missing: tuple[str, ...]
    rule_version: str = RULE_VERSION


@dataclass(frozen=True)
class Quarantined:
    isin: str
    reason: QuarantineReason
    quote: str | None
    as_of: datetime
    source_url: str
    rule_version: str = RULE_VERSION


@dataclass(frozen=True)
class RevenueQuarter:
    period_start: date
    period_end: date
    revenue_inr: Decimal
    as_of: datetime


@dataclass(frozen=True)
class TrailingRevenue:
    revenue_inr: Decimal
    period_end: date
    as_of: datetime  # when the last of the four quarters became public


@dataclass(frozen=True)
class OrderIntensity:
    isin: str
    order_value_inr: Decimal
    trailing_revenue_inr: Decimal | None
    trailing_period_end: date | None
    execution_months: Decimal | None
    pct_of_trailing_revenue: Decimal | None
    annualised_pct: Decimal | None
    gaps: tuple[str, ...]
    as_of: datetime
    rule_version: str = RULE_VERSION


def extract_order_win(announcement: Announcement, t: datetime) -> OrderWin | Quarantined:
    """The order-win fact in `announcement`, or the reason there is none."""
    require_aware(t, "t")
    require_aware(announcement.as_of, "announcement.as_of")
    require_aware(announcement.announced_on, "announcement.announced_on")
    if announcement.as_of > t:
        raise LookAhead(f"announcement as_of {announcement.as_of} is after t {t}")

    text = f"{announcement.subject} {announcement.description or ''}".strip()
    c = order_terms.classify(text)

    def quarantine(reason: QuarantineReason, quote: str | None = None) -> Quarantined:
        return Quarantined(announcement.isin, reason, quote, announcement.as_of, announcement.source_url)

    if c.verdict is order_terms.Verdict.NO_MATCH:
        return quarantine(QuarantineReason.NOT_AN_ORDER)
    if c.verdict is order_terms.Verdict.PRE_AWARD:
        return quarantine(QuarantineReason.PRE_AWARD, c.pre_awards[0].quote)
    if c.verdict is order_terms.Verdict.EXCLUDED:
        return quarantine(QuarantineReason.EXCLUDED, c.exclusions[0].quote)
    if c.verdict is order_terms.Verdict.AMBIGUOUS:
        first = (c.awards or c.pre_awards or c.exclusions)[0]
        return quarantine(QuarantineReason.AMBIGUOUS, first.quote)

    if not c.amounts:
        return quarantine(QuarantineReason.NO_VALUE, c.awards[0].quote)
    if len({a.inr for a in c.amounts}) > 1:
        return quarantine(QuarantineReason.MULTIPLE_VALUES, " | ".join(a.quote for a in c.amounts))
    amount = c.amounts[0]

    counterparty = _counterparty(text)
    months, end, period_quote = _execution_period(text, announcement.announced_on.date())

    missing = []
    if counterparty is None:
        missing.append("counterparty")
    if months is None:
        missing.append("execution_period")

    return OrderWin(
        isin=announcement.isin,
        announced_on=announcement.announced_on,
        as_of=announcement.as_of,
        source_url=announcement.source_url,
        order_value_inr=amount.inr,
        value_quote=amount.quote,
        counterparty=counterparty,
        execution_months=months,
        execution_end=end,
        period_quote=period_quote,
        matched_phrase=c.awards[0].phrase,
        missing=tuple(missing),
    )


def _counterparty(text: str) -> str | None:
    m = _COUNTERPARTY_RE.search(text)
    if m is None:
        return None
    name = m.group(1).rstrip(".,;:")
    return name or None


def _execution_period(
    text: str, announced_on: date
) -> tuple[Decimal | None, date | None, str | None]:
    """(months, end date, quote). A stated duration wins over an end date."""
    end, end_quote = _execution_end(text)
    if end is not None and end <= announced_on:
        end, end_quote = None, None  # an end before the announcement is not a period

    d = _DURATION_RE.search(text)
    if d is not None:
        n = Decimal(d.group(1))
        months = n * 12 if d.group(2).lower().startswith("year") else n
        return months, end, d.group(0)
    if end is not None:
        return Decimal((end - announced_on).days) / DAYS_PER_MONTH, end, end_quote
    return None, None, None


def _days_in_month(year: int, month: int) -> int:
    first_next = date(year + month // 12, month % 12 + 1, 1)
    return (first_next - date(year, month, 1)).days


def require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware, got naive {value!r}")
    return value


def _execution_end(text: str) -> tuple[date | None, str | None]:
    m = _END_DMY_RE.search(text)
    if m is not None:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))), m.group(0)
        except ValueError:
            return None, None
    m = _END_MY_RE.search(text)
    if m is not None:
        year, month = int(m.group(2)), _MONTHS[m.group(1).lower()]
        return date(year, month, _days_in_month(year, month)), m.group(0)
    return None, None


def trailing_revenue(quarters: Sequence[RevenueQuarter], t: datetime) -> TrailingRevenue | None:
    """Sum of the latest four contiguous quarters, or None if there are not four.

    Only quarter-length duration facts count; annual and cumulative periods
    are skipped so they can never be added to a quarter.
    """
    require_aware(t, "t")
    for q in quarters:
        require_aware(q.as_of, "quarter.as_of")
        if q.as_of > t:
            raise LookAhead(f"revenue for {q.period_end} as_of {q.as_of} is after t {t}")

    single = sorted(
        (q for q in quarters if (q.period_end - q.period_start).days + 1 in _QUARTER_DAYS),
        key=lambda q: q.period_end,
    )
    if len(single) < 4:
        return None
    last4 = single[-4:]
    for prev, nxt in zip(last4, last4[1:], strict=False):
        if nxt.period_start != prev.period_end + timedelta(days=1):
            return None
    return TrailingRevenue(
        revenue_inr=sum((q.revenue_inr for q in last4), Decimal(0)),
        period_end=last4[-1].period_end,
        as_of=max(q.as_of for q in last4),
    )


def order_intensity(win: OrderWin, revenue: TrailingRevenue | None, t: datetime) -> OrderIntensity:
    """Order value over trailing revenue, and annualised over the execution period."""
    require_aware(t, "t")
    if win.as_of > t:
        raise LookAhead(f"order win as_of {win.as_of} is after t {t}")
    if revenue is not None and revenue.as_of > t:
        raise LookAhead(f"trailing revenue as_of {revenue.as_of} is after t {t}")

    gaps: list[str] = []
    pct: Decimal | None = None
    annualised: Decimal | None = None

    if revenue is None or revenue.revenue_inr <= 0:
        gaps.append("trailing_revenue")
    else:
        pct = win.order_value_inr / revenue.revenue_inr * 100

    if win.execution_months is None:
        gaps.append("execution_period")
    elif pct is not None:
        annualised = pct / (win.execution_months / 12)

    return OrderIntensity(
        isin=win.isin,
        order_value_inr=win.order_value_inr,
        trailing_revenue_inr=revenue.revenue_inr if revenue is not None else None,
        trailing_period_end=revenue.period_end if revenue is not None else None,
        execution_months=win.execution_months,
        pct_of_trailing_revenue=pct,
        annualised_pct=annualised,
        gaps=tuple(gaps),
        as_of=max(win.as_of, revenue.as_of) if revenue is not None else win.as_of,
    )
