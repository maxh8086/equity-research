"""The Session 6 Screener comparison for one company, read at `t`, and its text rendering.

Every read goes through core/db/pit.py; every comparison is
core.compute.screener_compare. Consolidated only: the sample is compared on
the consolidated view, which is what Screener shows first.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from core.compute import ratios as r
from core.compute import screener_compare as sc
from core.compute.screener_compare import CRORE, Comparison, Status
from core.db.models import Consolidation
from core.db.pit import facts_as_of, financial_filings_as_of, ratios_as_of, screener_values_as_of
from ingest.screener_export.parser import RULE_VERSION as SCREENER_RULE_VERSION

CONS = Consolidation.CONSOLIDATED
LEASE_PAYMENTS = "payments_of_lease_liabilities_classified_as_financing_activities"
RATIO_LINES = frozenset({"ebit", "capital_employed"})  # Screener lines shown as money beside a ratio


def fiscal_year_end(year: int) -> date:
    return date(year, 3, 31)


@dataclass
class CompanyReport:
    isin: str
    symbol: str
    family: str | None = None
    export_as_of: datetime | None = None
    comparisons: list[Comparison] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    lease_payments: dict[date, Decimal] = field(default_factory=dict)

    @property
    def passes(self) -> bool:
        return not self.problems and sc.gate_passes(self.comparisons)


def _family(session: Session, isin: str, ends: list[date], as_of: datetime) -> str | None:
    """The taxonomy of the newest consolidated filing for one of the compared year ends."""
    filings = [f for f in financial_filings_as_of(session, isin=isin, as_of=as_of) if f.consolidation is CONS]
    chosen = [f for f in filings if f.period_end in ends] or filings
    return chosen[-1].taxonomy.value if chosen else None


def company_report(session: Session, *, isin: str, symbol: str, years: list[int], as_of: datetime) -> CompanyReport:
    report = CompanyReport(isin, symbol)
    ends = [fiscal_year_end(y) for y in years]

    export, values = screener_values_as_of(session, isin=isin, consolidation=CONS, as_of=as_of,
                                           rule_version=SCREENER_RULE_VERSION)  # fmt: skip
    if export is None:
        report.problems.append("no Screener export known at this as_of")
    else:
        report.export_as_of = export.as_of
    screener = {(v.statement, v.line, v.period_end): v.value for v in values}

    report.family = _family(session, isin, ends, as_of)
    if report.family is None:
        report.problems.append("no consolidated XBRL filing known at this as_of")
        return report

    ours: dict[sc.OursKey, Decimal] = {}
    for f in facts_as_of(session, isin=isin, consolidation=CONS, as_of=as_of):
        if f.period_start is None or r.is_fiscal_year(f.period_start, f.period_end):
            ours[(f.line_item, f.period_end)] = f.value
    report.lease_payments = {end: v for (item, end), v in ours.items() if item == LEASE_PAYMENTS}

    ours_ratios: dict[tuple[str, date], tuple[Decimal | None, str | None]] = {}
    if report.family in r.APPLICABLE_FAMILIES:
        for rv in ratios_as_of(session, isin=isin, consolidation=CONS, as_of=as_of):
            if rv.period_start is None or r.is_fiscal_year(rv.period_start, rv.period_end):
                ours_ratios[(rv.ratio, rv.period_end)] = (rv.value, rv.gap.value if rv.gap else None)

    for end in ends:
        report.comparisons += sc.compare_lines(report.family, screener, ours, end)
        if report.family in r.APPLICABLE_FAMILIES:
            report.comparisons += sc.compare_ratios(screener, ours_ratios, ours, end)
    return report


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _money(v: Decimal | None) -> str:
    return "-" if v is None else f"{v / CRORE:,.2f} cr"


def _fraction(v: Decimal | None) -> str:
    return "-" if v is None else f"{v:.4f}"


def _fmt(c: Comparison, v: Decimal | None) -> str:
    return _money(v) if c.kind == "line" else _fraction(v)


def render_comparison(c: Comparison) -> list[str]:
    gate = "" if c.gate else " (not gated)"
    out = [
        f"  FY{c.period_end.year % 100:02d} {c.kind} {c.name}: {c.status.value}{gate}  "
        f"screener {_fmt(c, c.screener)}  ours {_fmt(c, c.ours)}  diff {_fmt(c, c.difference)}  "
        f"tolerance {_fmt(c, c.tolerance)}"
    ]
    for side in c.sides:
        mark = "ok" if side.within else "--"
        absent = f"  absent: {', '.join(side.absent)}" if side.absent else ""
        out.append(f"      [{mark}] {side.label} = {_money(side.value)}{absent}")
    if c.kind == "ratio":
        shown = ", ".join(f"{name} {_money(v)}" for name, v in c.screener_inputs)
        out.append(f"      screener lines: {shown}")
    if c.note:
        out.append(f"      note: {c.note}")
    return out


def render_company(report: CompanyReport, *, show_all: bool) -> list[str]:
    verdict = "PASS" if report.passes else "FAIL"
    exported = report.export_as_of.isoformat() if report.export_as_of else "-"
    out = [f"{report.symbol} {report.isin}  family {report.family or '-'}  export {exported}  {verdict}"]
    out += [f"  problem: {p}" for p in report.problems]
    counts = Counter(c.status for c in report.comparisons if c.gate)
    if counts:
        out.append("  gated: " + ", ".join(f"{s.value} {n}" for s, n in sorted(counts.items())))
    for c in report.comparisons:
        if show_all or c.status is not Status.MATCH:
            out += render_comparison(c)
    return out


def render_findings(reports: list[CompanyReport]) -> list[str]:
    """What the sample says about the three Session 6 questions. Evidence only; no rule changes."""
    out = ["", "Lease liabilities (Screener borrowings minus ours, Ind AS families):"]
    for rep in reports:
        for c in rep.comparisons:
            if c.kind == "line" and c.name == "borrowings" and c.difference is not None and c.difference < 0:
                lease = rep.lease_payments.get(c.period_end)
                out.append(f"  {rep.symbol} FY{c.period_end.year % 100:02d}: Screener higher by "
                           f"{_money(-c.difference)}; lease payments that year {_money(lease)}")  # fmt: skip

    out += ["", "Non-controlling interests (which of our sums Screener's line agrees with):"]
    for name in ("net_profit", "equity"):
        tally: Counter[str] = Counter()
        for rep in reports:
            for c in rep.comparisons:
                if c.kind != "line" or c.name != name or len(c.sides) < 2:
                    continue
                primary, alternates = c.sides[0].within, any(s.within for s in c.sides[1:])
                tally[{(True, True): "both (no NCI to tell apart)", (True, False): "including NCI",
                       (False, True): "owners only", (False, False): "neither"}[primary, alternates]] += 1  # fmt: skip
        out.append(f"  {name}: " + (", ".join(f"{k} {n}" for k, n in sorted(tally.items())) or "no evidence"))

    out += [
        "",
        "Average or closing capital employed: the Data Sheet has no ratios, so both ROCE rows are Screener's lines",
        "through our formulas. They check our ROCE inputs, not Screener's choice; that needs Screener's displayed",
        "ROCE and stays open.",
    ]
    return out
