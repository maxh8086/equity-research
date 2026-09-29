"""Corporate-action lifecycle, dilution arithmetic and the demerger milestone chain.

Pure and deterministic (R1). Three parts:

* Lifecycle: which status changes between versions of one action are legal
  (`ANNOUNCED -> APPROVED -> DATES_SET -> EFFECTIVE -> COMPLETED`, with
  `REVISED` and `WITHDRAWN` reachable from any non-terminal status).
* Dilution: the CLAUDE.md dilution facts (dilution %, issue-price discount,
  pro-forma EPS, return versus earnings yield). Functions return facts and
  booleans, never an action word; every threshold is a required parameter.
* Demerger chain: milestone dates in, `scheduled_event` row plans out, with
  severity computed here. The entitlement ratio adjusts no price until it is
  exchange-parsed or human-verified.

Not covered here: use-of-proceeds claims (a model-extracted `guidance_claim`,
which needs the announcement text) and whether use of proceeds is "vague".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

RULE_VERSION = "corporate_action_lifecycle/1"

_ZERO = Decimal(0)


class NotComputable(ValueError):
    """The inputs exist but the quantity has no meaning (e.g. EPS of zero or below)."""


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

_RANK = {"announced": 0, "approved": 1, "dates_set": 2, "effective": 3, "completed": 4}
_TERMINAL = frozenset({"completed", "withdrawn"})


@dataclass(frozen=True)
class ActionVersion:
    """One stored version of an action. `seq` breaks ties between equal `as_of`."""

    action_key: str
    status: str
    as_of: datetime
    seq: int


@dataclass(frozen=True)
class TransitionViolation:
    action_key: str
    previous: str
    new: str
    as_of: datetime


def is_allowed_transition(previous: str, new: str) -> bool:
    """Whether a version with status `new` may follow one with status `previous`.

    Forward steps may skip (exchange feeds often publish dates first); a
    restatement in the same status is allowed (new terms, a verified ratio);
    going back is not; nothing follows COMPLETED or WITHDRAWN. `previous` must
    be a ranked status or WITHDRAWN: a REVISED predecessor is resolved to the
    status before it by `lifecycle_violations`.
    """
    if previous in _TERMINAL:
        return False
    if new in ("withdrawn", "revised"):
        return True
    if previous == "revised":
        return True
    return _RANK[new] >= _RANK[previous]


def _ordered(versions: Iterable[ActionVersion]) -> list[ActionVersion]:
    return sorted(versions, key=lambda v: (v.as_of, v.seq))


def current_status(versions: Iterable[ActionVersion]) -> str | None:
    """The newest version's status: latest `as_of`, then highest `seq`."""
    ordered = _ordered(versions)
    return ordered[-1].status if ordered else None


def lifecycle_violations(versions: Iterable[ActionVersion]) -> list[TransitionViolation]:
    """Illegal status changes, per `action_key`, in the order the versions were known.

    A REVISED version does not reset progress: the next version is compared with
    the last non-REVISED status before it.
    """
    by_key: dict[str, list[ActionVersion]] = {}
    for version in versions:
        by_key.setdefault(version.action_key, []).append(version)

    out: list[TransitionViolation] = []
    for key in sorted(by_key):
        anchor: str | None = None  # last non-revised status
        for version in _ordered(by_key[key]):
            if anchor is not None and not is_allowed_transition(anchor, version.status):
                out.append(TransitionViolation(key, anchor, version.status, version.as_of))
            if version.status != "revised":
                anchor = version.status
    return out


# --------------------------------------------------------------------------- #
# Dilution
# --------------------------------------------------------------------------- #


def dilution_fraction(shares_outstanding: int, new_shares: int, *, unconverted_dilutive: int = 0) -> Decimal:
    """New shares as a fraction of the enlarged count.

    The base includes unconverted warrants and options (`unconverted_dilutive`),
    as CLAUDE.md requires: `new / (outstanding + unconverted + new)`.
    """
    if shares_outstanding <= 0 or new_shares <= 0 or unconverted_dilutive < 0:
        raise ValueError("shares_outstanding and new_shares must be positive, unconverted_dilutive not negative")
    return Decimal(new_shares) / Decimal(shares_outstanding + unconverted_dilutive + new_shares)


def issue_discount(issue_price: Decimal, market_price: Decimal) -> Decimal:
    """`(market - issue) / market`: positive below market, negative at a premium."""
    if market_price <= _ZERO or issue_price < _ZERO:
        raise ValueError("market_price must be positive and issue_price not negative")
    return (market_price - issue_price) / market_price


@dataclass(frozen=True)
class ProForma:
    proceeds: Decimal
    eps_before: Decimal
    eps_after: Decimal
    eps_dilution_fraction: Decimal  # (before - after) / before; negative when EPS rises
    earnings_yield_at_issue: Decimal  # eps_before / issue_price
    return_rate: Decimal
    return_exceeds_earnings_yield: bool  # strictly greater


def pro_forma_eps(
    *,
    earnings: Decimal,
    shares_outstanding: int,
    new_shares: int,
    issue_price: Decimal,
    return_rate: Decimal,
    unconverted_dilutive: int = 0,
) -> ProForma:
    """EPS before and after an issue, crediting the new money with `return_rate`.

    `return_rate` is caller-supplied: the expected return on the new money, or
    the interest saved per rupee of proceeds if it repays debt (see
    `return_rate_from_debt_repayment`). Both share counts include unconverted
    warrants and options. Raises `NotComputable` when earnings or the price
    make EPS or the earnings yield meaningless.
    """
    if shares_outstanding <= 0 or new_shares <= 0 or unconverted_dilutive < 0:
        raise ValueError("shares_outstanding and new_shares must be positive, unconverted_dilutive not negative")
    if issue_price <= _ZERO:
        raise NotComputable("issue price must be positive")
    base = shares_outstanding + unconverted_dilutive
    eps_before = earnings / Decimal(base)
    if eps_before <= _ZERO:
        raise NotComputable("pro-forma EPS needs positive earnings")
    proceeds = Decimal(new_shares) * issue_price
    eps_after = (earnings + proceeds * return_rate) / Decimal(base + new_shares)
    earnings_yield = eps_before / issue_price
    return ProForma(
        proceeds=proceeds,
        eps_before=eps_before,
        eps_after=eps_after,
        eps_dilution_fraction=(eps_before - eps_after) / eps_before,
        earnings_yield_at_issue=earnings_yield,
        return_rate=return_rate,
        return_exceeds_earnings_yield=return_rate > earnings_yield,
    )


def return_rate_from_debt_repayment(proceeds: Decimal, debt_repaid: Decimal, interest_rate: Decimal) -> Decimal:
    """Interest saved as a rate on the whole proceeds: `debt_repaid * interest_rate / proceeds`."""
    if proceeds <= _ZERO or debt_repaid < _ZERO:
        raise ValueError("proceeds must be positive and debt_repaid not negative")
    if debt_repaid > proceeds:
        raise ValueError("debt_repaid exceeds proceeds")
    return debt_repaid * interest_rate / proceeds


def eps_dilution_beyond_threshold(pro_forma: ProForma, *, threshold: Decimal) -> bool:
    """EPS dilution strictly beyond `threshold` with no offsetting return.

    The threshold is caller-supplied (CLAUDE.md names none). True means the
    review condition holds; what to do about it is not decided here.
    """
    return pro_forma.eps_dilution_fraction > threshold and not pro_forma.return_exceeds_earnings_yield


@dataclass(frozen=True)
class IssuePriceFacts:
    discount: Decimal
    at_or_above_market: bool
    discount_at_least_steep: bool
    promoter_allottee: bool | None  # None: the source does not say


def issue_price_facts(
    *,
    issue_price: Decimal,
    market_price: Decimal,
    allottee_is_promoter: bool | None,
    steep_discount: Decimal,
) -> IssuePriceFacts:
    """Discount versus market, and the flags the CLAUDE.md dilution rules read."""
    discount = issue_discount(issue_price, market_price)
    return IssuePriceFacts(
        discount=discount,
        at_or_above_market=issue_price >= market_price,
        discount_at_least_steep=discount >= steep_discount,
        promoter_allottee=allottee_is_promoter,
    )


def dilution_event_count(dates: Iterable[date], *, on: date, window_days: int) -> int:
    """Dilution events dated within `[on - window_days, on]`; later events never count."""
    if window_days < 0:
        raise ValueError("window_days must not be negative")
    start = on - timedelta(days=window_days)
    return sum(1 for d in dates if start <= d <= on)


# --------------------------------------------------------------------------- #
# Demerger milestone chain
# --------------------------------------------------------------------------- #


class DemergerMilestone(StrEnum):
    SCHEME = "scheme"
    BOARD = "board"
    SHAREHOLDER = "shareholder"
    CREDITOR = "creditor"
    NCLT_ORDER = "nclt_order"
    RECORD_DATE = "record_date"
    LISTING = "listing"


DEMERGER_CHAIN: tuple[DemergerMilestone, ...] = tuple(DemergerMilestone)

# Values of core.db.models.ScheduledEventType (a test keeps the two in step).
MILESTONE_EVENT_TYPE: dict[DemergerMilestone, str] = {
    DemergerMilestone.SCHEME: "demerger_scheme",
    DemergerMilestone.BOARD: "demerger_board",
    DemergerMilestone.SHAREHOLDER: "demerger_shareholder",
    DemergerMilestone.CREDITOR: "demerger_creditor",
    DemergerMilestone.NCLT_ORDER: "demerger_nclt_order",
    DemergerMilestone.RECORD_DATE: "record_date",
    DemergerMilestone.LISTING: "demerger_listing",
}


@dataclass(frozen=True)
class MilestonePlan:
    milestone: DemergerMilestone
    event_type: str
    event_date: date
    days_until: int  # negative once past
    reached: bool  # event date before `today`
    severity: str  # critical | high | medium | low
    out_of_order: bool  # dated before an earlier milestone in the chain


def plan_demerger_chain(
    known: Mapping[DemergerMilestone, date],
    *,
    today: date,
    held: bool,
    critical_within_days: int,
    high_within_days: int,
) -> list[MilestonePlan]:
    """One `scheduled_event` plan per known milestone, in chain order.

    Severity: a milestone already past is LOW; for a stock not held it is LOW
    (a watchlist item); for a holding it is CRITICAL within `critical_within_days`,
    HIGH within `high_within_days`, else MEDIUM. Both windows are caller-supplied.
    A date earlier than that of an earlier milestone is flagged, not corrected.
    """
    if critical_within_days < 0 or high_within_days < critical_within_days:
        raise ValueError("need 0 <= critical_within_days <= high_within_days")
    rows: list[MilestonePlan] = []
    latest: date | None = None
    for milestone in DEMERGER_CHAIN:
        when = known.get(milestone)
        if when is None:
            continue
        days = (when - today).days
        if days < 0 or not held:
            severity = "low"
        elif days <= critical_within_days:
            severity = "critical"
        elif days <= high_within_days:
            severity = "high"
        else:
            severity = "medium"
        rows.append(
            MilestonePlan(
                milestone=milestone,
                event_type=MILESTONE_EVENT_TYPE[milestone],
                event_date=when,
                days_until=days,
                reached=days < 0,
                severity=severity,
                out_of_order=latest is not None and when < latest,
            )
        )
        latest = when if latest is None else max(latest, when)
    return rows


def entitlement_adjusts_price(ratio_basis: str) -> bool:
    """A demerger entitlement ratio adjusts a price only if exchange-parsed or human-verified."""
    return ratio_basis in ("exchange_field", "human_verified")
