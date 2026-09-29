"""Ownership rules, remainder of CLAUDE.md "Ownership, index and corporate-action signals".

Sibling of ownership_rules.py (which holds the false-signal filters, net aggregation and the
pledge-invoked alert). This module adds the rules that need a window, a quarterly series or holdings:

- promoter open-market buying above a threshold, and selling, over a rolling window
- FII plus DII share count rising or falling for N consecutive quarters, with rises in a quarter
  of an effective index inclusion tagged as passive, not conviction
- pledge created and pledge released; pledged percentage rising, critical for a holding above Y points
- promoter open-market sale above X percent of the stake, critical for a holding
- promoter selling clustered shortly before results

Pure functions over plain frozen inputs; no I/O. Every threshold (X, Y, N, window, cluster size) is a
required caller argument and is never defaulted. Holdings are a plain tuple of HoldingInput, to be fed
by a broker adapter later. Outputs are facts with the rule_version and evidence strings; CRITICAL and
EXIT_REVIEW are review flags, never verdicts, and holdings are never removed automatically.
Insufficient data returns NotEvaluableResult, never a guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from core.compute.ownership_rules import (
    InsiderTradeInput,
    NotEvaluableResult,
    StakeDisclosureInput,
    filter_false_signals_insider_trades,
)

RULE_VERSION = "ownership_trends/1"

CRITICAL_FLAGS = ("CRITICAL", "EXIT_REVIEW")
PROMOTER_CATEGORY = "Promoter"
FII_CATEGORY = "institutions_foreign"
DII_CATEGORY = "institutions_domestic"
PROMOTER_PATTERN_CATEGORY = "promoter_group"
SHARES_MEASURE = "total_shares"
PLEDGED_MEASURE = "pledged_shares"


@dataclass(frozen=True)
class HoldingInput:
    """One position held by the user. Plain input; a broker adapter feeds it later."""

    isin: str
    quantity: Decimal


@dataclass(frozen=True)
class PatternFactInput:
    """One shareholding-pattern count for one ISIN, mirroring a ShareholdingPattern row."""

    as_on_date: date
    category: str
    measure: str
    value: Decimal


@dataclass(frozen=True)
class IndexEventInput:
    """One index-event row, mirroring IndexEvent (enum values as plain strings)."""

    isin: str
    index_code: str
    event_type: str
    status: str
    effective_date: date | None


@dataclass(frozen=True)
class OwnershipFact:
    """A computed ownership fact. Direction and severity only; never a verdict."""

    isin: str
    fact_type: str
    direction: str  # "inflow" or "outflow"
    severity: str  # "critical", "high", "medium", "low"
    event_date: date
    value: Decimal | None
    detail: str
    flags: tuple[str, ...]
    evidence: tuple[str, ...]
    rule_version: str = RULE_VERSION


Result = OwnershipFact | NotEvaluableResult


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _positive_int(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _non_negative(name: str, value: Decimal) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")


def _quarter_index(day: date) -> int:
    return day.year * 4 + (day.month - 1) // 3


def _previous_quarter_end(period_end: date) -> date:
    first_of_quarter = date(period_end.year, ((period_end.month - 1) // 3) * 3 + 1, 1)
    return first_of_quarter - timedelta(days=1)


def _trade_evidence(t: InsiderTradeInput) -> str:
    return f"insider_trade:{t.isin}:{t.trade_date.isoformat()}:{t.person_name}:{t.quantity}"


def _promoter_open_market(trades: Iterable[InsiderTradeInput], isin: str) -> list[InsiderTradeInput]:
    """Promoter trades of `isin` that the false-signal filter passes as open-market signals."""
    mine = [t for t in trades if t.isin == isin and t.person_category == PROMOTER_CATEGORY]
    return [
        c.trade
        for c in filter_false_signals_insider_trades(mine)
        if c.classification == "signal_eligible" and isinstance(c.trade, InsiderTradeInput)
    ]


def _held(isin: str, holdings: Sequence[HoldingInput]) -> bool:
    return any(h.isin == isin and h.quantity > 0 for h in holdings)


def _by_date(facts: Iterable[PatternFactInput], category: str, measure: str) -> dict[date, Decimal]:
    return {f.as_on_date: f.value for f in facts if f.category == category and f.measure == measure}


def _not_evaluable(isin: str, event_type: str, reason: str, day: date) -> NotEvaluableResult:
    return NotEvaluableResult(isin=isin, event_type=event_type, reason=reason, event_date=day)


# --------------------------------------------------------------------------- #
# promoter open-market flow over a rolling window
# --------------------------------------------------------------------------- #


def promoter_open_market_flow(
    trades: Sequence[InsiderTradeInput],
    *,
    isin: str,
    as_of_date: date,
    window_days: int,
    inflow_threshold_quantity: Decimal,
) -> list[OwnershipFact]:
    """Net promoter open-market quantity over the `window_days` ending `as_of_date`.

    Net buying strictly above the threshold is a positive inflow fact. Net selling is a negative
    outflow fact at any size (CLAUDE.md gives selling no threshold). Gifts, ESOS, allotments,
    unclassified modes and non-promoters never count.
    """
    _positive_int("window_days", window_days)
    _non_negative("inflow_threshold_quantity", inflow_threshold_quantity)
    start = as_of_date - timedelta(days=window_days)
    in_window = [t for t in _promoter_open_market(trades, isin) if start < t.trade_date <= as_of_date]
    if not in_window:
        return []
    net = sum((t.quantity for t in in_window), Decimal("0"))
    evidence = tuple(_trade_evidence(t) for t in in_window)
    if net > inflow_threshold_quantity:
        return [
            OwnershipFact(
                isin=isin, fact_type="promoter_open_market_buying", direction="inflow",
                severity="medium", event_date=as_of_date, value=net,
                detail=f"net {net} promoter open-market shares over {window_days} days, "
                f"above threshold {inflow_threshold_quantity}",
                flags=(), evidence=evidence,
            )  # fmt: skip
        ]
    if net < 0:
        return [
            OwnershipFact(
                isin=isin, fact_type="promoter_open_market_selling", direction="outflow",
                severity="medium", event_date=as_of_date, value=net,
                detail=f"net {net} promoter open-market shares over {window_days} days",
                flags=(), evidence=evidence,
            )  # fmt: skip
        ]
    return []


# --------------------------------------------------------------------------- #
# FII plus DII trend and passive-inflow tagging
# --------------------------------------------------------------------------- #


def passive_inflow_periods(
    events: Iterable[IndexEventInput], *, isin: str, period_ends: Iterable[date]
) -> frozenset[date]:
    """Period ends whose quarter contains an effective index inclusion of `isin`.

    Institutional buying in such a quarter is tagged passive, not read as conviction.
    """
    effective = [
        e.effective_date
        for e in events
        if e.isin == isin and e.event_type == "inclusion" and e.status == "effective"
        and e.effective_date is not None
    ]  # fmt: skip
    return frozenset(
        end
        for end in period_ends
        if any(_previous_quarter_end(end) < d <= end for d in effective)
    )


def fii_dii_trend(
    facts: Sequence[PatternFactInput],
    *,
    isin: str,
    n_quarters: int,
    passive_periods: frozenset[date],
) -> list[Result]:
    """FII plus DII share count rising, or falling, for `n_quarters` consecutive quarters.

    `facts` are one ISIN's shareholding-pattern counts (measure total_shares). Needs
    n_quarters + 1 consecutive quarter-ends with both categories present. A rise whose changes include
    a `passive_periods` quarter is returned as a low-severity passive tag instead of a rise.
    """
    _positive_int("n_quarters", n_quarters)
    fii = _by_date(facts, FII_CATEGORY, SHARES_MEASURE)
    dii = _by_date(facts, DII_CATEGORY, SHARES_MEASURE)
    totals = {d: fii[d] + dii[d] for d in sorted(fii.keys() & dii.keys())}
    dates = list(totals)[-(n_quarters + 1) :]
    anchor = dates[-1] if dates else max((f.as_on_date for f in facts), default=date.min)
    if len(dates) < n_quarters + 1:
        return [
            _not_evaluable(
                isin, "fii_dii_trend",
                f"insufficient quarters with both FII and DII counts: need {n_quarters + 1}, have {len(dates)}",
                anchor,
            )  # fmt: skip
        ]
    if any(_quarter_index(b) - _quarter_index(a) != 1 for a, b in zip(dates, dates[1:])):
        return [_not_evaluable(isin, "fii_dii_trend", "quarters are not consecutive", anchor)]
    deltas = [totals[b] - totals[a] for a, b in zip(dates, dates[1:])]
    evidence = tuple(f"shareholding_pattern:{isin}:{d.isoformat()}:fii+dii={totals[d]}" for d in dates)
    change = totals[dates[-1]] - totals[dates[0]]
    if all(d > 0 for d in deltas):
        passive = any(later in passive_periods for later in dates[1:])
        return [
            OwnershipFact(
                isin=isin,
                fact_type="fii_dii_rise_passive_tagged" if passive else "fii_dii_rising",
                direction="inflow", severity="low" if passive else "medium",
                event_date=dates[-1], value=change,
                detail=f"FII plus DII shares up {n_quarters} consecutive quarters"
                + (", including an index-inclusion quarter (passive, not conviction)" if passive else ""),
                flags=(), evidence=evidence,
            )  # fmt: skip
        ]
    if all(d < 0 for d in deltas):
        return [
            OwnershipFact(
                isin=isin, fact_type="fii_dii_exiting", direction="outflow", severity="medium",
                event_date=dates[-1], value=change,
                detail=f"FII plus DII shares down {n_quarters} consecutive quarters",
                flags=(), evidence=evidence,
            )  # fmt: skip
        ]
    return []


# --------------------------------------------------------------------------- #
# pledges
# --------------------------------------------------------------------------- #


def pledge_event_facts(disclosures: Sequence[StakeDisclosureInput], *, isin: str) -> list[OwnershipFact]:
    """Pledge released (positive) and pledge created (negative). Invocation is in ownership_rules."""
    kinds = {
        "pledge_released": ("inflow", "pledged shares released"),
        "pledge_created": ("outflow", "pledge created"),
    }
    out = []
    for d in disclosures:
        if d.isin != isin or d.disclosure_type not in kinds:
            continue
        direction, text = kinds[d.disclosure_type]
        out.append(
            OwnershipFact(
                isin=isin, fact_type=d.disclosure_type, direction=direction, severity="medium",
                event_date=d.disclosure_date, value=d.shares_acquired,
                detail=f"{text} by {d.acquirer_name}", flags=(),
                evidence=(f"stake_disclosure:{isin}:{d.disclosure_date.isoformat()}:{d.disclosure_type}:{d.acquirer_name}",),
            )  # fmt: skip
        )
    return out


def pledged_pct_change_facts(
    facts: Sequence[PatternFactInput],
    *,
    isin: str,
    y_points: Decimal,
    holdings: Sequence[HoldingInput],
) -> list[Result]:
    """Pledged share of the promoter stake, quarter on quarter.

    Any rise is a negative fact. A rise of more than `y_points` percentage points on a holding is
    critical with CRITICAL and EXIT_REVIEW flags; on a non-holding it is high, unflagged.
    """
    _non_negative("y_points", y_points)
    total = _by_date(facts, PROMOTER_PATTERN_CATEGORY, SHARES_MEASURE)
    pledged = _by_date(facts, PROMOTER_PATTERN_CATEGORY, PLEDGED_MEASURE)
    pct = {d: pledged[d] * 100 / total[d] for d in sorted(total.keys() & pledged.keys()) if total[d] > 0}
    dates = list(pct)
    if len(dates) < 2:
        anchor = max((f.as_on_date for f in facts), default=date.min)
        return [
            _not_evaluable(
                isin, "pledged_pct_change",
                "insufficient quarters with promoter total and pledged share counts", anchor,
            )  # fmt: skip
        ]
    held = _held(isin, holdings)
    out: list[Result] = []
    for a, b in zip(dates, dates[1:]):
        if _quarter_index(b) - _quarter_index(a) != 1:
            out.append(_not_evaluable(isin, "pledged_pct_change", "quarters are not consecutive", b))
            continue
        rise = pct[b] - pct[a]
        if rise <= 0:
            continue
        critical = rise > y_points and held
        out.append(
            OwnershipFact(
                isin=isin, fact_type="pledged_pct_rising", direction="outflow",
                severity="critical" if critical else ("high" if rise > y_points else "medium"),
                event_date=b, value=rise,
                detail=f"pledged share of promoter stake up {rise} points ({pct[a]} to {pct[b]}), Y={y_points}",
                flags=CRITICAL_FLAGS if critical else (),
                evidence=tuple(f"shareholding_pattern:{isin}:{d.isoformat()}:pledged_pct={pct[d]}" for d in (a, b)),
            )  # fmt: skip
        )
    return out


# --------------------------------------------------------------------------- #
# promoter sale above X percent of stake
# --------------------------------------------------------------------------- #


def promoter_sale_pct_of_stake_facts(
    trades: Sequence[InsiderTradeInput],
    facts: Sequence[PatternFactInput],
    *,
    isin: str,
    x_pct: Decimal,
    holdings: Sequence[HoldingInput],
) -> list[Result]:
    """Each promoter open-market sale as a percent of the promoter stake at the latest quarter-end
    on or before the trade. Above `x_pct` on a holding: critical with CRITICAL and EXIT_REVIEW flags;
    on a non-holding: high, unflagged. No stake on file is not-evaluable.
    """
    _non_negative("x_pct", x_pct)
    stakes = _by_date(facts, PROMOTER_PATTERN_CATEGORY, SHARES_MEASURE)
    held = _held(isin, holdings)
    out: list[Result] = []
    for t in _promoter_open_market(trades, isin):
        if t.quantity >= 0:
            continue
        known = [d for d in stakes if d <= t.trade_date and stakes[d] > 0]
        if not known:
            out.append(
                _not_evaluable(isin, "promoter_sale_pct_of_stake", "no promoter stake on or before the trade", t.trade_date)
            )
            continue
        stake_date = max(known)
        pct = -t.quantity * 100 / stakes[stake_date]
        if pct <= x_pct:
            continue
        out.append(
            OwnershipFact(
                isin=isin, fact_type="promoter_sale_above_x", direction="outflow",
                severity="critical" if held else "high", event_date=t.trade_date, value=pct,
                detail=f"promoter sold {pct}% of stake of {stakes[stake_date]} as on {stake_date.isoformat()}, X={x_pct}",
                flags=CRITICAL_FLAGS if held else (),
                evidence=(_trade_evidence(t), f"shareholding_pattern:{isin}:{stake_date.isoformat()}:promoter={stakes[stake_date]}"),
            )  # fmt: skip
        )
    return out


# --------------------------------------------------------------------------- #
# promoter selling clustered before results
# --------------------------------------------------------------------------- #


def promoter_selling_before_results(
    trades: Sequence[InsiderTradeInput],
    results_dates: Sequence[date],
    *,
    isin: str,
    window_days: int,
    min_trades: int,
) -> list[OwnershipFact]:
    """For each results date, promoter open-market sales in the `window_days` up to it (inclusive).

    At least `min_trades` sales is a cluster: a negative outflow fact.
    """
    _positive_int("window_days", window_days)
    _positive_int("min_trades", min_trades)
    sales = [t for t in _promoter_open_market(trades, isin) if t.quantity < 0]
    out = []
    for results_date in sorted(set(results_dates)):
        start = results_date - timedelta(days=window_days)
        cluster = [t for t in sales if start <= t.trade_date <= results_date]
        if len(cluster) < min_trades:
            continue
        net = sum((t.quantity for t in cluster), Decimal("0"))
        out.append(
            OwnershipFact(
                isin=isin, fact_type="promoter_selling_before_results", direction="outflow",
                severity="high", event_date=results_date, value=net,
                detail=f"{len(cluster)} promoter open-market sales within {window_days} days before results",
                flags=(), evidence=tuple(_trade_evidence(t) for t in cluster),
            )  # fmt: skip
        )
    return out
