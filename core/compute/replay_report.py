"""Render a replay as plain text. Deterministic, no model involved.

This is the counterfactual view a person reads: what the rules would have
raised, what the gates blocked, and what the price then did. It is a text
projection of `core.compute.replay` values, not prose generation, so it lives
in `core/compute/` and not in `narrate/`.

The disclaimer is not optional. `render` always emits it and
`tests/test_replay_report.py` fails if it stops doing so.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal

from core.compute.disclaimer import disclaimer
from core.compute.replay import (
    GateOutcome,
    HitRate,
    Outcome,
    Raised,
    ReviewAction,
    Window,
)

RULE_VERSION = "replay_report/1"

_HEADING = "Replay — what the review rules would have raised"
# The three-letter verdict vocabulary is kept out of this text on purpose, so
# that `test_no_rendered_report_contains_a_buy_sell_or_hold_verdict` can be an
# absolute check on the rendered bytes with no prose to exempt.
_NOTE = (
    "Counterfactual. These are the system's three review actions: not orders, "
    "and not a trading recommendation. Forward returns below are outcome data: "
    "they are shown here and are never an input to a forward-looking judgement."
)


def render(
    raised: Iterable[Raised],
    as_of: date,
    outcomes: Iterable[Outcome] = (),
    rates: Iterable[HitRate] = (),
    evaluation: Window | None = None,
    tuning: Window | None = None,
) -> str:
    """The whole replay as text, ending in the disclaimer for `as_of`."""
    by_key: dict[tuple[str, date, ReviewAction], list[Outcome]] = {}
    for o in outcomes:
        by_key.setdefault((o.isin, o.raised_on, o.action), []).append(o)

    lines = [_HEADING, "=" * len(_HEADING), "", _NOTE, ""]
    if tuning is not None and evaluation is not None:
        lines += [
            f"Parameters tuned on [{tuning.start} .. {tuning.end}), "
            f"reported on [{evaluation.start} .. {evaluation.end}).",
            "",
        ]

    ordered = sorted(raised, key=lambda r: (r.raised_on, r.isin, r.action))
    if not ordered:
        lines.append("No signals in the replay window.")
    for r in ordered:
        lines.extend(_review_lines(r, by_key.get((r.isin, r.raised_on, r.action), ())))

    rates = list(rates)
    if rates:
        lines += ["", "Measured hit rates", "------------------"]
        lines.extend(_rate_line(h) for h in rates)

    lines += ["", disclaimer(as_of)]
    return "\n".join(lines)


def _review_lines(r: Raised, outcomes: Iterable[Outcome]) -> list[str]:
    verdict = "would have raised" if r.would_have_raised else "blocked"
    head = f"{r.raised_on}  {r.isin}  {r.action.value}  — {verdict}"
    lines = ["", head, "-" * len(head)]
    lines.append(f"  categories: {', '.join(c.value for c in r.categories) or 'none'}")
    for g in r.gates:
        mark = {GateOutcome.PASS: "pass", GateOutcome.BLOCK: "BLOCK", GateOutcome.UNKNOWN: "unknown"}[
            g.outcome
        ]
        lines.append(f"  gate {g.gate.value}: {mark} ({g.reason})")
    for url in r.evidence_urls:
        lines.append(f"  evidence: {url}")
    for o in sorted(outcomes, key=lambda o: o.horizon_days):
        lines.append(f"  outcome @{o.horizon_days}d ({o.observed_on}): {_outcome_text(o)}")
    lines.append(f"  rule_version: {r.rule_version}")
    return lines


def _outcome_text(o: Outcome) -> str:
    text = f"{o.close_at_raise} -> {o.close_at_horizon}, {_signed(o.return_pct)}%"
    if o.excess_return_pct is not None:
        text += f" ({_signed(o.excess_return_pct)}% vs benchmark)"
    return text


def _rate_line(h: HitRate) -> str:
    pct = (h.rate * Decimal(100)).quantize(Decimal("0.1"))
    which = "raised" if h.would_have_raised else "blocked by a gate"
    return (
        f"  {h.action.value} @{h.horizon_days}d ({which}), {h.basis.value} return "
        f"vs {_signed(h.threshold_pct)}%: {h.hits}/{h.raised} = {pct}%"
    )


def _signed(value: Decimal) -> str:
    return f"+{value}" if value > 0 else str(value)


def render_isin_index(raised: Iterable[Raised]) -> Mapping[str, int]:
    """How many reviews each ISIN would have had raised. Blocked ones excluded."""
    counts: dict[str, int] = {}
    for r in raised:
        if r.would_have_raised:
            counts[r.isin] = counts.get(r.isin, 0) + 1
    return dict(sorted(counts.items()))
