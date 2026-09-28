"""What the review rules would have raised on a past date, and what happened next.

This is the counterfactual view: not a recommendation, and deliberately not a
three-letter verdict. The system has three actions and they are named
`ADD_REVIEW`, `TRIM_REVIEW` and `EXIT_REVIEW` (CLAUDE.md, Decision support).
A replay row says *the rules would have raised an ADD_REVIEW here, on these
two categories, and the price was X twelve months later* — never a verdict
word, which `tests/test_architecture.py` enforces on this package.

Two halves, kept structurally apart because R2 depends on it:

- **Raising** (`replay`, `raise_on`) sees only what was known at the raise
  date. `LookAhead` is raised if a caller passes a signal or a state observed
  after it. `Raised` has no outcome field, so an outcome cannot reach a
  prompt through it.
- **Measuring** (`measure`, `hit_rates`) sees the future of a raise and is
  outcome data: displayable, never an input to a forward-looking judgement.

Gates apply to `ADD_REVIEW` only, per CLAUDE.md. A blocked signal is still
returned, carrying the gate that blocked it — the point of the exercise is to
see what the gates cost, so they are never silently dropped.

Pure: callers pass everything in. `core.db.pit` supplies the point-in-time
reads; adjusted closes come from `pit.adjusted_closes_as_of`, so the
adjustment factors are themselves only those known at `t`.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

RULE_VERSION = "replay/1"

# Research decision, not config (CLAUDE.md: trigger thresholds are hardcoded
# under a rule_version). Two independent categories, per the ADD_REVIEW gates.
MIN_INDEPENDENT_CATEGORIES = 2

# A move in the direction the review implied counts. Zero, not a margin: a
# margin chosen before any replay has run would be a parameter fitted to
# nothing. Callers tune it on the tuning window and report on the evaluation
# window, which `hit_rates` enforces.
DEFAULT_HIT_THRESHOLD_PCT = Decimal("0")

_PCT = Decimal("0.01")
_RATE = Decimal("0.0001")


class ReviewAction(StrEnum):
    """The only three actions this system has. None of them is a trading verdict."""

    ADD_REVIEW = "ADD_REVIEW"
    TRIM_REVIEW = "TRIM_REVIEW"
    EXIT_REVIEW = "EXIT_REVIEW"


class SignalCategory(StrEnum):
    """Independent categories for the two-category gate.

    Independence is the point: two ownership signals are one category, so they
    cannot satisfy the gate between them.
    """

    OWNERSHIP = "ownership"
    VALUATION = "valuation"
    EXPANSION = "expansion"
    FUNDAMENTAL_UPGRADE = "fundamental_upgrade"
    CREDIT_RATING = "credit_rating"
    MACRO_EXPOSURE = "macro_exposure"
    CORPORATE_ACTION = "corporate_action"
    INDEX_EVENT = "index_event"
    DILUTION = "dilution"
    THESIS = "thesis"
    CONCENTRATION = "concentration"
    DRAWDOWN = "drawdown"


class Gate(StrEnum):
    TWO_INDEPENDENT_CATEGORIES = "two_independent_categories"
    PRICE_BELOW_BASE_OR_BULL = "price_below_base_or_bull"
    THESIS_INTACT = "thesis_intact"
    CONCENTRATION_WITHIN_CAP = "concentration_within_cap"


class GateOutcome(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    UNKNOWN = "unknown"  # the inputs to decide it were not known at the raise


class ReturnBasis(StrEnum):
    ABSOLUTE = "absolute"
    EXCESS = "excess"  # over the benchmark, which is the honest read for equities


class LookAhead(ValueError):
    """Evidence dated after the raise reached the raising side. Always a bug."""


class NotYetObservable(ValueError):
    """An outcome was asked for before its observation date had passed."""


class OverlappingWindows(ValueError):
    """Parameters were tuned on the period they are reported on."""


@dataclass(frozen=True)
class Signal:
    """One deterministic trigger, as it stood on `observed_on`."""

    isin: str
    observed_on: date
    category: SignalCategory
    action: ReviewAction
    rule_version: str
    evidence_url: str


@dataclass(frozen=True)
class KnownState:
    """Everything the gates need, as known at `as_of`.

    Every field is `None` when it was not known, and a gate whose inputs are
    missing reports UNKNOWN rather than guessing in either direction.
    Valuations are the code-computed bear/base/bull values.
    """

    as_of: date
    close: Decimal | None = None
    base_value: Decimal | None = None
    bull_value: Decimal | None = None
    thesis_intact: bool | None = None
    weight_pct: Decimal | None = None
    cap_pct: Decimal | None = None


@dataclass(frozen=True)
class GateResult:
    gate: Gate
    outcome: GateOutcome
    reason: str


@dataclass(frozen=True)
class Raised:
    """What the rules would have raised on `raised_on`, and what stopped them.

    Deliberately carries no outcome: see the module docstring. `blocked_by` is
    empty exactly when the review would have been raised.
    """

    isin: str
    raised_on: date
    action: ReviewAction
    categories: tuple[SignalCategory, ...]
    gates: tuple[GateResult, ...]
    evidence_urls: tuple[str, ...]
    rule_version: str = RULE_VERSION

    @property
    def blocked_by(self) -> tuple[Gate, ...]:
        return tuple(g.gate for g in self.gates if g.outcome is not GateOutcome.PASS)

    @property
    def would_have_raised(self) -> bool:
        return not self.blocked_by


@dataclass(frozen=True)
class Outcome:
    """Outcome data. Displayable; never an input to a forward-looking judgement."""

    isin: str
    raised_on: date
    action: ReviewAction
    horizon_days: int
    would_have_raised: bool  # False if the gates blocked it; scored separately
    observed_on: date
    close_at_raise: Decimal
    close_at_horizon: Decimal
    return_pct: Decimal
    benchmark_return_pct: Decimal | None
    excess_return_pct: Decimal | None
    rule_version: str = RULE_VERSION

    def by(self, basis: ReturnBasis) -> Decimal | None:
        return self.return_pct if basis is ReturnBasis.ABSOLUTE else self.excess_return_pct


@dataclass(frozen=True)
class HitRate:
    action: ReviewAction
    horizon_days: int
    would_have_raised: bool
    basis: ReturnBasis
    threshold_pct: Decimal
    raised: int
    hits: int
    rate: Decimal


@dataclass(frozen=True)
class Window:
    """A half-open date interval [start, end)."""

    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    def overlaps(self, other: Window) -> bool:
        return self.start < other.end and other.start < self.end


# --- raising: knowledge at t only -------------------------------------------


def raise_on(
    isin: str,
    day: date,
    signals: Sequence[Signal],
    state: KnownState,
) -> list[Raised]:
    """The reviews the rules would have raised for `isin` on `day`.

    `signals` are that day's live signals and `state` is what was known then.
    Both are checked against `day`: anything dated later is `LookAhead`.
    """
    if state.as_of > day:
        raise LookAhead(f"{isin}: state as_of {state.as_of} is after the raise date {day}")
    late = [s for s in signals if s.observed_on > day]
    if late:
        raise LookAhead(
            f"{isin}: {len(late)} signal(s) observed after {day}, "
            f"earliest {min(s.observed_on for s in late)}"
        )

    out: list[Raised] = []
    for action in ReviewAction:
        group = [s for s in signals if s.action is action and s.isin == isin]
        if not group:
            continue
        categories = tuple(sorted({s.category for s in group}))
        gates = _gates(action, categories, state)
        out.append(
            Raised(
                isin=isin,
                raised_on=day,
                action=action,
                categories=categories,
                gates=gates,
                evidence_urls=tuple(sorted({s.evidence_url for s in group})),
            )
        )
    return out


def replay(
    signals: Iterable[Signal],
    states: Mapping[tuple[str, date], KnownState],
) -> list[Raised]:
    """Every review the rules would have raised, oldest first.

    Signals are grouped by (ISIN, observation date); `states` supplies what was
    known for each such pair. A pair with no state is replayed against an empty
    state, so its gates report UNKNOWN rather than being dropped — a gap in the
    stores must be visible, not silently favourable.
    """
    grouped: dict[tuple[str, date], list[Signal]] = defaultdict(list)
    for s in signals:
        grouped[(s.isin, s.observed_on)].append(s)

    out: list[Raised] = []
    for (isin, day), group in sorted(grouped.items()):
        state = states.get((isin, day), KnownState(as_of=day))
        out.extend(raise_on(isin, day, group, state))
    return sorted(out, key=lambda r: (r.raised_on, r.isin, r.action))


def _gates(
    action: ReviewAction,
    categories: tuple[SignalCategory, ...],
    state: KnownState,
) -> tuple[GateResult, ...]:
    """The ADD_REVIEW gates. TRIM and EXIT reviews are not gated (CLAUDE.md)."""
    if action is not ReviewAction.ADD_REVIEW:
        return ()
    return (
        _gate_categories(categories),
        _gate_valuation(state),
        _gate_thesis(state),
        _gate_concentration(state),
    )


def _gate_categories(categories: tuple[SignalCategory, ...]) -> GateResult:
    n = len(categories)
    ok = n >= MIN_INDEPENDENT_CATEGORIES
    return GateResult(
        Gate.TWO_INDEPENDENT_CATEGORIES,
        GateOutcome.PASS if ok else GateOutcome.BLOCK,
        f"{n} independent categor{'y' if n == 1 else 'ies'}: "
        f"{', '.join(c.value for c in categories) or 'none'}",
    )


def _gate_valuation(state: KnownState) -> GateResult:
    ceiling = state.bull_value if state.base_value is None else state.base_value
    if state.close is None or ceiling is None:
        return GateResult(
            Gate.PRICE_BELOW_BASE_OR_BULL,
            GateOutcome.UNKNOWN,
            "no close or no computed base/bull value known at the raise",
        )
    ok = state.close < ceiling
    return GateResult(
        Gate.PRICE_BELOW_BASE_OR_BULL,
        GateOutcome.PASS if ok else GateOutcome.BLOCK,
        f"close {state.close} {'<' if ok else '>='} {ceiling}",
    )


def _gate_thesis(state: KnownState) -> GateResult:
    if state.thesis_intact is None:
        return GateResult(Gate.THESIS_INTACT, GateOutcome.UNKNOWN, "no thesis evaluated at the raise")
    return GateResult(
        Gate.THESIS_INTACT,
        GateOutcome.PASS if state.thesis_intact else GateOutcome.BLOCK,
        "thesis intact" if state.thesis_intact else "a thesis condition had failed",
    )


def _gate_concentration(state: KnownState) -> GateResult:
    if state.weight_pct is None or state.cap_pct is None:
        return GateResult(
            Gate.CONCENTRATION_WITHIN_CAP,
            GateOutcome.UNKNOWN,
            "no holding weight or no cap known at the raise",
        )
    ok = state.weight_pct <= state.cap_pct
    return GateResult(
        Gate.CONCENTRATION_WITHIN_CAP,
        GateOutcome.PASS if ok else GateOutcome.BLOCK,
        f"weight {state.weight_pct}% {'<=' if ok else '>'} cap {state.cap_pct}%",
    )


# --- measuring: outcome data ------------------------------------------------


def measure(
    raised: Raised,
    closes: Mapping[date, Decimal],
    horizon_days: int,
    measured_at: date,
    benchmark: Mapping[date, Decimal] | None = None,
) -> Outcome:
    """What the price did over `horizon_days` after `raised`.

    `closes` are adjusted closes for the ISIN across its lineage; `benchmark`
    is the same for the index, when an excess read is wanted. Both are looked
    up on the first trading day on or after the target date, so a holiday does
    not silently shorten the horizon.

    `NotYetObservable` if the horizon had not passed by `measured_at`: an
    outcome may only be read once its observation date is history.
    """
    if horizon_days <= 0:
        raise ValueError(f"horizon_days must be positive, got {horizon_days}")

    at_raise, raise_day = _close_on_or_after(closes, raised.raised_on)
    target = raised.raised_on + timedelta(days=horizon_days)
    at_horizon, observed_on = _close_on_or_after(closes, target)
    if observed_on > measured_at:
        raise NotYetObservable(
            f"{raised.isin}: {horizon_days}d horizon from {raised.raised_on} "
            f"lands on {observed_on}, after the measurement date {measured_at}"
        )
    if observed_on <= raise_day:
        raise ValueError(f"{raised.isin}: horizon close {observed_on} is not after {raise_day}")

    ret = _pct_change(at_raise, at_horizon)
    bench = excess = None
    if benchmark is not None:
        b0, _ = _close_on_or_after(benchmark, raised.raised_on)
        b1, _ = _close_on_or_after(benchmark, target)
        bench = _pct_change(b0, b1)
        excess = (ret - bench).quantize(_PCT)

    return Outcome(
        isin=raised.isin,
        raised_on=raised.raised_on,
        action=raised.action,
        horizon_days=horizon_days,
        would_have_raised=raised.would_have_raised,
        observed_on=observed_on,
        close_at_raise=at_raise,
        close_at_horizon=at_horizon,
        return_pct=ret,
        benchmark_return_pct=bench,
        excess_return_pct=excess,
    )


def hit_rates(
    outcomes: Iterable[Outcome],
    evaluation: Window,
    tuning: Window,
    basis: ReturnBasis = ReturnBasis.ABSOLUTE,
    threshold_pct: Decimal = DEFAULT_HIT_THRESHOLD_PCT,
) -> list[HitRate]:
    """Hit rate per action and horizon, over the evaluation window only.

    `tuning` is the window the thresholds were chosen on. It is a required
    argument rather than a convention because reporting a hit rate on the
    period the parameters were fitted to is the overfitting CLAUDE.md forbids;
    an overlap is `OverlappingWindows`, not a warning.

    A hit is a move of at least `threshold_pct` in the direction the review
    implied: up for `ADD_REVIEW`, down for `TRIM_REVIEW` and `EXIT_REVIEW`.

    Reviews the gates blocked are reported as their own rows, never merged into
    the raised ones. The comparison between the two is what says whether a gate
    earns its place; averaging them together hides exactly that.
    """
    if evaluation.overlaps(tuning):
        raise OverlappingWindows(
            f"evaluation [{evaluation.start}, {evaluation.end}) overlaps "
            f"tuning [{tuning.start}, {tuning.end})"
        )

    buckets: dict[tuple[ReviewAction, int, bool], list[Outcome]] = defaultdict(list)
    for o in outcomes:
        if evaluation.contains(o.raised_on):
            buckets[(o.action, o.horizon_days, o.would_have_raised)].append(o)

    out: list[HitRate] = []
    for (action, horizon, was_raised), group in sorted(buckets.items()):
        scored = [o for o in group if o.by(basis) is not None]
        if basis is ReturnBasis.EXCESS and len(scored) != len(group):
            raise ValueError(
                f"{action} @{horizon}d: {len(group) - len(scored)} outcome(s) "
                "have no benchmark, so an excess hit rate cannot be computed"
            )
        hits = sum(1 for o in scored if _is_hit(action, o.by(basis), threshold_pct))
        rate = (
            (Decimal(hits) / Decimal(len(scored))).quantize(_RATE) if scored else Decimal("0.0000")
        )
        out.append(
            HitRate(action, horizon, was_raised, basis, threshold_pct, len(scored), hits, rate)
        )
    return out


def _is_hit(action: ReviewAction, ret: Decimal | None, threshold_pct: Decimal) -> bool:
    if ret is None:
        return False
    if action is ReviewAction.ADD_REVIEW:
        return ret >= threshold_pct
    return ret <= -threshold_pct


def _close_on_or_after(closes: Mapping[date, Decimal], day: date) -> tuple[Decimal, date]:
    days = sorted(closes)
    i = bisect.bisect_left(days, day)
    if i == len(days):
        raise NotYetObservable(f"no close on or after {day}; series ends {days[-1] if days else None}")
    found = days[i]
    return closes[found], found


def _pct_change(start: Decimal, end: Decimal) -> Decimal:
    if start <= 0:
        raise ValueError(f"close must be positive to compute a return, got {start}")
    return ((end - start) / start * Decimal(100)).quantize(_PCT)
