"""The guidance report: what management promised, what landed, what went silent.

Pure assembly and rendering (R1). Every number in the output is a stored fact,
a parsed claim value, or arithmetic done here; the model writes none of it. The
report is a function of rows already read at one `as_of` (core/db/pit.py,
`guidance_report_as_of`), so it is as replayable as the reads under it (R2).

A figure without a source is not rendered. `SourceRef` and `Figure` refuse to
exist without a URL and a timezone-aware date, so an unsourced number is a
`ValueError` at construction, before any layout is chosen.

No comparison with consensus estimates: there is no free Indian consensus
feed. Nothing here says whether to act on a delivery rate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from core.compute.guidance import HedgeStrength, Unit
from core.compute.guidance_resolution import DeliveryRate, Status, UnresolvableReason

RULE_VERSION = "guidance_report/1"

_CRORE = Decimal("10000000")  # facts are absolute rupees; the report speaks crore
_TENTH = Decimal("0.1")
_ONE = Decimal("1")

_SourceKey = tuple[str, datetime]


@dataclass(frozen=True)
class SourceRef:
    """A dated link: where a figure was published, and when we could first know it."""

    url: str
    as_of: datetime

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("a source needs a URL")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("a source date must be timezone-aware")

    @property
    def key(self) -> _SourceKey:
        return (self.url, self.as_of)


@dataclass(frozen=True)
class Figure:
    """One number shown in the report, with the fact it came from."""

    label: str
    value: Decimal
    unit: Unit
    source: SourceRef | None

    def __post_init__(self) -> None:
        if self.source is None:
            raise ValueError(f"figure {self.label!r} has no source")


@dataclass(frozen=True)
class InputFact:
    """A stored fact an actual was built from: absolute rupees, and its filing."""

    line_item: str
    period_end: date
    value: Decimal
    source: SourceRef | None


@dataclass(frozen=True)
class ClaimView:
    """One claim as read at the report's `as_of`, with its grade and silence."""

    metric: str
    period_label: str
    call_date: date
    hedge: HedgeStrength
    hedge_verbatim: str
    guided_low: Decimal | None
    guided_high: Decimal | None
    unit: Unit
    claim_source: SourceRef
    status: Status
    reason: UnresolvableReason | None
    actual: Decimal | None
    actual_unit: Unit | None
    period_end: date | None
    inputs: tuple[InputFact, ...]
    silent: bool


@dataclass(frozen=True)
class ReportRow:
    claim: ClaimView
    guided: str
    actual: Figure | None
    prior: Figure | None


@dataclass(frozen=True)
class GuidanceReport:
    isin: str
    symbol: str
    as_of: datetime
    quarter_start: date
    headline: str
    rows: tuple[ReportRow, ...]
    new_this_quarter: tuple[ReportRow, ...]
    silent: tuple[ReportRow, ...]
    delivery: DeliveryRate
    sources: tuple[SourceRef, ...]


def format_value(value: Decimal, unit: Unit) -> str:
    """A number as the report prints it. Nothing is rounded before this."""
    if unit is Unit.PERCENT:
        return f"{value.quantize(_TENTH, ROUND_HALF_UP)}%"
    if unit is Unit.BPS:
        return f"{value.quantize(_ONE, ROUND_HALF_UP)} bps"
    if unit is Unit.INR_CRORE:
        return f"₹{value.quantize(_ONE, ROUND_HALF_UP):,} cr"
    if unit is Unit.MULTIPLE:
        return f"{value.quantize(_TENTH, ROUND_HALF_UP)}x"
    return f"{value.quantize(_ONE, ROUND_HALF_UP):,}"


def _guided(claim: ClaimView) -> str:
    if claim.guided_low is None:
        return "directional"
    low = format_value(claim.guided_low, claim.unit)
    if claim.guided_high is None or claim.guided_high == claim.guided_low:
        return low
    return f"{low} to {format_value(claim.guided_high, claim.unit)}"


def _prior(claim: ClaimView) -> Figure | None:
    """The latest sourced input from a period before the claim's own, in crore.

    Only where the grade was built from one (a growth metric reads last year's
    figure). Otherwise the report shows a dash, never a number it cannot source.
    """
    if claim.period_end is None:
        return None
    earlier = [f for f in claim.inputs if f.period_end < claim.period_end and f.source is not None]
    if not earlier:
        return None
    fact = max(earlier, key=lambda f: f.period_end)
    return Figure(
        f"{fact.line_item} for the period ending {fact.period_end.isoformat()}",
        fact.value / _CRORE,
        Unit.INR_CRORE,
        fact.source,
    )


def _actual(claim: ClaimView) -> Figure | None:
    if claim.actual is None or claim.actual_unit is None:
        return None
    sourced = [f.source for f in claim.inputs if f.source is not None]
    if not sourced:
        return None  # a computed actual whose inputs carry no source is not shown
    latest = max(sourced, key=lambda s: s.as_of)
    return Figure(f"{claim.metric} {claim.period_label}", claim.actual, claim.actual_unit, latest)


def _headline(delivery: DeliveryRate, symbol: str, as_of: datetime, rows: int) -> str:
    day = as_of.date().isoformat()
    if rows == 0:
        return f"{symbol}: no guidance on record as of {day}."
    graded = delivery.met + delivery.missed
    if delivery.rate is None:
        return (
            f"{symbol}: {rows} guidance claims on record as of {day}, none graded yet "
            f"({delivery.open} open, {delivery.unresolvable} not gradable, {delivery.silent} silent)."
        )
    pct = (delivery.rate * 100).quantize(_ONE, ROUND_HALF_UP)
    return (
        f"{symbol}: hedge-weighted delivery rate {pct}% over {graded} graded claims "
        f"({delivery.met} met, {delivery.missed} missed); {delivery.open} open, "
        f"{delivery.unresolvable} not gradable, {delivery.silent} silent, as of {day}."
    )


def build_report(
    *,
    isin: str,
    symbol: str,
    as_of: datetime,
    quarter_start: date,
    claims: Sequence[ClaimView],
    delivery: DeliveryRate,
) -> GuidanceReport:
    """Assemble the report from claims read at `as_of`; nothing is looked up here."""
    rows = tuple(ReportRow(c, _guided(c), _actual(c), _prior(c)) for c in claims)
    seen: dict[_SourceKey, SourceRef] = {}
    for row in rows:
        refs = [row.claim.claim_source]
        refs += [f.source for f in row.claim.inputs if f.source is not None]
        for ref in refs:
            seen.setdefault(ref.key, ref)
    sources = tuple(sorted(seen.values(), key=lambda s: (s.as_of, s.url)))
    return GuidanceReport(
        isin=isin,
        symbol=symbol,
        as_of=as_of,
        quarter_start=quarter_start,
        headline=_headline(delivery, symbol, as_of, len(rows)),
        rows=rows,
        new_this_quarter=tuple(r for r in rows if r.claim.call_date >= quarter_start),
        silent=tuple(r for r in rows if r.claim.silent),
        delivery=delivery,
        sources=sources,
    )


def _status(row: ReportRow) -> str:
    claim = row.claim
    if claim.silent:
        return "silent"
    if claim.status is Status.UNRESOLVABLE and claim.reason is not None:
        return f"not gradable ({claim.reason.value})"
    return claim.status.value


def _cell(figure: Figure | None, n: dict[_SourceKey, int]) -> str:
    if figure is None or figure.source is None:
        return "—"
    return f"{format_value(figure.value, figure.unit)} [{n[figure.source.key]}]"


def _table(rows: Sequence[ReportRow], n: dict[_SourceKey, int]) -> list[str]:
    out = [
        "| Metric | Period | Said | Guided | Actual | Prior period | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        c = row.claim
        out.append(
            f"| {c.metric} | {c.period_label} | {c.hedge_verbatim} ({c.hedge.value}) "
            f"[{n[c.claim_source.key]}] | {row.guided} | {_cell(row.actual, n)} "
            f"| {_cell(row.prior, n)} | {_status(row)} |"
        )
    return out


def render_markdown(report: GuidanceReport) -> str:
    """The report as an earnings note. Every bracketed number is a line in Sources."""
    n = {s.key: i for i, s in enumerate(report.sources, start=1)}
    lines = [f"# {report.symbol} guidance report", "", f"**{report.headline}**", ""]
    lines += [f"## New since {report.quarter_start.isoformat()}", ""]
    lines += _table(report.new_this_quarter, n) if report.new_this_quarter else ["No new guidance this period."]
    lines += ["", "## Every claim: guided, actual and prior period", ""]
    lines += _table(report.rows, n) if report.rows else ["No guidance on record."]
    lines += ["", "## Went silent", ""]
    if report.silent:
        lines += [
            f"- {r.claim.metric} {r.claim.period_label}: guided {r.guided} on "
            f"{r.claim.call_date.isoformat()}, absent from the calls since [{n[r.claim.claim_source.key]}]"
            for r in report.silent
        ]
    else:
        lines.append("Nothing has gone silent.")
    lines += ["", "## Sources", ""]
    lines += [f"{i}. {s.url} (public {s.as_of.date().isoformat()})" for i, s in enumerate(report.sources, start=1)]
    if not report.sources:
        lines.append("None.")
    lines += ["", "Silence is counted apart from misses and is not in the delivery rate.", ""]
    return "\n".join(lines)
