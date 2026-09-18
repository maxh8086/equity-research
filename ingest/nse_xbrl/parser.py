"""Parse one SEBI results XBRL instance (NSE/BSE) into facts. Pure: no I/O, no DB.

File shape (checked against the contract fixtures):
- Contexts are named by column and period type: `OneD`, `OneI`, `FourD`,
  `FourI`, ... Column One is the quarter being reported, Four the year to
  date. Contexts carrying a segment or scenario are dimensional (segments,
  expense breakdowns): they are counted and deferred, not parsed yet.
- A column's reporting period is stated by the `DateOfStartOfReportingPeriod`
  and `DateOfEndOfReportingPeriod` facts in that column's non-dimensional
  contexts. The XBRL period of a year-to-date duration context repeats the
  quarter's dates, so it is not used; only its end date is checked.
- Instant (balance sheet) facts are dated at the column end; the XBRL
  instant must equal it.
- Values are full rupees ("342570000000.00"); `decimals` states the rounding
  (-7 for crores, -5 for lakhs) loosely, so it is not checked against them.

Whole-file problems raise FileRejected; single-fact problems become
FactIssue entries and the rest of the file is kept. Nothing is guessed.
"""

import io
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from posixpath import basename

from core.db.models import Consolidation, FinancialFactsQuarantineReason as Reason
from ingest.nse_xbrl import mapping
from ingest.nse_xbrl.mapping import Taxonomy

XBRLI = "{http://www.xbrl.org/2003/instance}"
LINK = "{http://www.xbrl.org/2003/linkbase}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

COLUMN_CONTEXT = re.compile(r"^(One|Two|Three|Four|Five|Six)(D|I)$")
PLAIN_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# financial_facts.value is NUMERIC(28, 6); anything finer would be rounded silently.
STORED_FRACTION_DIGITS = 6
STORED_INTEGER_DIGITS = 22


class FileRejected(Exception):
    def __init__(self, reason: Reason, detail: str):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ParsedFact:
    xbrl_element: str  # "<taxonomy prefix>:<local name>"
    line_item: str
    period_start: date | None  # None for instant facts
    period_end: date
    value: Decimal
    unit: str
    context_refs: tuple[str, ...]  # every context that reported this value


@dataclass(frozen=True)
class FactIssue:
    xbrl_element: str
    context_ref: str
    raw_value: str
    reason: Reason
    detail: str


@dataclass(frozen=True)
class ParsedFiling:
    taxonomy: Taxonomy
    symbol: str | None
    isin: str | None  # as written in the file; only bank and insurer files carry one
    scrip_code: str | None
    consolidation: Consolidation
    reporting_quarter: str
    period_start: date  # column One: the quarter being reported
    period_end: date
    board_meeting_date: date
    facts: tuple[ParsedFact, ...]
    issues: tuple[FactIssue, ...]
    dimensional_facts_deferred: int


@dataclass(frozen=True)
class _Context:
    column: str | None  # None for dimensional contexts
    instant: date | None
    end: date | None  # endDate of a duration context


def parse_filing(data: bytes) -> ParsedFiling:
    root, namespaces = _load(data)
    taxonomy = _detect_taxonomy(root, namespaces)
    contexts = _contexts(root)
    units = _units(root, namespaces)

    meta: dict[str, set[str]] = defaultdict(set)
    column_bounds: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    numeric: list[ET.Element] = []
    deferred = 0
    identifiers = {
        (ident.get("scheme"), (ident.text or "").strip()) for ident in root.iter(f"{XBRLI}identifier")
    }

    for el in root:
        ref = el.get("contextRef")
        if ref is None:
            continue
        ctx = contexts.get(ref)
        if ctx is None:
            raise FileRejected(Reason.SHAPE_CHANGED, f"fact {el.tag} refers to unknown context {ref!r}")
        if el.get("unitRef") is not None:
            if ctx.column is None:
                deferred += 1
            else:
                numeric.append(el)
            continue
        if ctx.column is None or el.get(XSI_NIL) == "true":
            continue
        local = _local(el.tag)
        text = (el.text or "").strip()
        if local in (mapping.META_START, mapping.META_END):
            column_bounds[ctx.column][local].add(text)
        else:
            meta[local].add(text)

    periods = _column_periods(column_bounds)
    if "One" not in periods:
        raise FileRejected(Reason.PERIOD_UNCONFIRMED, "no reporting period stated for column One")

    currency = _single(meta, mapping.META_CURRENCY, required=False)
    if currency is not None and currency != "INR":
        raise FileRejected(Reason.SHAPE_CHANGED, f"presentation currency {currency!r}, expected INR")
    nature = _single(meta, mapping.META_NATURE)
    if nature.lower() not in mapping.NATURE:
        raise FileRejected(Reason.SHAPE_CHANGED, f"unknown standalone/consolidated value {nature!r}")

    facts, issues = _classify(numeric, taxonomy, contexts, periods, units)
    start, end = periods["One"]
    return ParsedFiling(
        taxonomy=taxonomy,
        symbol=_symbol(meta, identifiers),
        isin=_single(meta, mapping.META_ISIN, required=False),
        scrip_code=_single(meta, mapping.META_SCRIP_CODE, required=False),
        consolidation=Consolidation(mapping.NATURE[nature.lower()]),
        reporting_quarter=_single(meta, mapping.META_QUARTER),
        period_start=start,
        period_end=end,
        board_meeting_date=_date(_single(meta, mapping.META_BOARD_MEETING), mapping.META_BOARD_MEETING),
        facts=facts,
        issues=issues,
        dimensional_facts_deferred=deferred,
    )


def _load(data: bytes) -> tuple[ET.Element, dict[str, str]]:
    # Stdlib ElementTree expands internal entities; results filings never declare any.
    head = data.lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise FileRejected(Reason.SHAPE_CHANGED, "document type or entity declarations are not accepted")
    namespaces: dict[str, str] = {}
    try:
        for _event, (prefix, uri) in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
            if namespaces.get(prefix, uri) != uri:
                raise FileRejected(Reason.SHAPE_CHANGED, f"namespace prefix {prefix!r} declared twice")
            namespaces[prefix] = uri
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FileRejected(Reason.SHAPE_CHANGED, f"not well-formed XML: {exc}") from exc
    if root.tag != f"{XBRLI}xbrl":
        raise FileRejected(Reason.SHAPE_CHANGED, f"root element is {root.tag}, not xbrli:xbrl")
    return root, namespaces


def _detect_taxonomy(root: ET.Element, namespaces: dict[str, str]) -> Taxonomy:
    refs = [basename(ref.get(XLINK_HREF) or "") for ref in root.iter(f"{LINK}schemaRef")]
    if len(refs) != 1:
        raise FileRejected(Reason.SHAPE_CHANGED, f"expected one schemaRef, found {refs}")
    declared = set(namespaces.values())
    matches = [t for t in mapping.TAXONOMIES if t.schema_ref == refs[0] and t.entry_namespace in declared]
    if len(matches) != 1:
        raise FileRejected(Reason.UNSUPPORTED_TAXONOMY, f"schemaRef {refs[0]!r} with no supported entry point")
    return matches[0]


def _contexts(root: ET.Element) -> dict[str, _Context]:
    contexts = {}
    for ctx in root.iter(f"{XBRLI}context"):
        cid = ctx.get("id") or ""
        dimensional = ctx.find(f"{XBRLI}entity/{XBRLI}segment") is not None or (
            ctx.find(f"{XBRLI}scenario") is not None
        )
        period = ctx.find(f"{XBRLI}period")
        instant = period.find(f"{XBRLI}instant") if period is not None else None
        end = period.find(f"{XBRLI}endDate") if period is not None else None
        if instant is None and end is None:
            raise FileRejected(Reason.SHAPE_CHANGED, f"context {cid!r} has no period")
        column = None
        if not dimensional:
            match = COLUMN_CONTEXT.match(cid)
            if match is None:
                raise FileRejected(Reason.SHAPE_CHANGED, f"unexpected non-dimensional context id {cid!r}")
            if (match.group(2) == "I") != (instant is not None):
                raise FileRejected(Reason.SHAPE_CHANGED, f"context {cid!r} period type does not match its id")
            column = match.group(1)
        contexts[cid] = _Context(
            column=column,
            instant=_date(instant.text, cid) if instant is not None else None,
            end=_date(end.text, cid) if end is not None else None,
        )
    return contexts


def _units(root: ET.Element, namespaces: dict[str, str]) -> dict[str, str | None]:
    """unit id -> our unit name, or None for a unit we do not recognise."""

    def measures(parent: ET.Element | None) -> tuple[tuple[str, str], ...]:
        if parent is None:
            return ()
        out = []
        for m in parent.findall(f"{XBRLI}measure"):
            prefix, _, local = (m.text or "").strip().rpartition(":")
            out.append((namespaces.get(prefix, prefix), local))
        return tuple(sorted(out))

    units = {}
    for unit in root.iter(f"{XBRLI}unit"):
        divide = unit.find(f"{XBRLI}divide")
        if divide is None:
            key = (measures(unit), ())
        else:
            key = (
                measures(divide.find(f"{XBRLI}unitNumerator")),
                measures(divide.find(f"{XBRLI}unitDenominator")),
            )
        units[unit.get("id") or ""] = mapping.UNITS.get(key)
    return units


def _column_periods(bounds: dict[str, dict[str, set[str]]]) -> dict[str, tuple[date, date]]:
    periods = {}
    for column, found in bounds.items():
        starts, ends = found.get(mapping.META_START, set()), found.get(mapping.META_END, set())
        if len(starts) != 1 or len(ends) != 1:
            raise FileRejected(
                Reason.PERIOD_UNCONFIRMED,
                f"column {column}: start {sorted(starts)}, end {sorted(ends)}; need exactly one of each",
            )
        start = _date(next(iter(starts)), f"{column} start")
        end = _date(next(iter(ends)), f"{column} end")
        if start > end:
            raise FileRejected(Reason.PERIOD_UNCONFIRMED, f"column {column} starts after it ends")
        periods[column] = (start, end)
    return periods


def _classify(
    numeric: list[ET.Element],
    taxonomy: Taxonomy,
    contexts: dict[str, _Context],
    periods: dict[str, tuple[date, date]],
    units: dict[str, str | None],
) -> tuple[tuple[ParsedFact, ...], tuple[FactIssue, ...]]:
    issues: list[FactIssue] = []
    candidates: dict[tuple, list[tuple[ParsedFact, str]]] = defaultdict(list)
    line_items = mapping.LINE_ITEMS[taxonomy.namespace]

    for el in numeric:
        ref = el.get("contextRef") or ""
        ctx = contexts[ref]
        namespace, local = _split(el.tag)
        element = f"{taxonomy.prefix}:{local}" if namespace == taxonomy.namespace else el.tag
        raw = (el.text or "").strip()

        def issue(reason: Reason, detail: str) -> None:
            issues.append(FactIssue(element, ref, raw, reason, detail))

        if el.get(XSI_NIL) == "true":
            continue
        if ctx.column not in periods:
            raise FileRejected(Reason.PERIOD_UNCONFIRMED, f"column {ctx.column} holds facts but states no period")
        start, end = periods[ctx.column]
        if (ctx.instant or ctx.end) != end:
            raise FileRejected(
                Reason.PERIOD_UNCONFIRMED,
                f"context {ref} ends {ctx.instant or ctx.end}, but column {ctx.column} ends {end}",
            )
        mapped = line_items.get(local) if namespace == taxonomy.namespace else None
        if mapped is None:
            issue(Reason.UNMAPPED_ELEMENT, "element not in the mapping table")
            continue
        line_item, expected_unit = mapped
        unit = units.get(el.get("unitRef") or "")
        if unit != expected_unit:
            issue(Reason.UNEXPECTED_UNIT, f"unit {el.get('unitRef')!r} resolves to {unit}, expected {expected_unit}")
            continue
        if not PLAIN_DECIMAL.match(raw):
            issue(Reason.MALFORMED_VALUE, "not a plain decimal number")
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            issue(Reason.MALFORMED_VALUE, "not a plain decimal number")
            continue
        scale_problem = _scale_problem(value, el.get("decimals"))
        if scale_problem:
            issue(Reason.UNEXPECTED_SCALE, scale_problem)
            continue
        fact = ParsedFact(
            xbrl_element=element,
            line_item=line_item,
            period_start=None if ctx.instant is not None else start,
            period_end=end,
            value=value,
            unit=unit,
            context_refs=(ref,),
        )
        candidates[(line_item, fact.period_start, fact.period_end)].append((fact, raw))

    facts = []
    for group in candidates.values():
        values = {fact.value for fact, _raw in group}
        if len(values) == 1:
            first = group[0][0]
            refs = tuple(sorted({ref for fact, _raw in group for ref in fact.context_refs}))
            facts.append(
                ParsedFact(
                    first.xbrl_element, first.line_item, first.period_start, first.period_end,
                    first.value, first.unit, refs,
                )  # fmt: skip
            )
            continue
        for fact, raw in group:
            issues.append(
                FactIssue(
                    fact.xbrl_element, fact.context_refs[0], raw, Reason.CONFLICTING_VALUES,
                    f"reported {len(group)} times for the same period with values {sorted(map(str, values))}",
                )  # fmt: skip
            )
    facts.sort(key=lambda f: (f.line_item, f.period_start or date.min, f.period_end))
    return tuple(facts), tuple(issues)


def _scale_problem(value: Decimal, decimals: str | None) -> str | None:
    """Whether the value fits the store and states its rounding.

    `decimals` is not checked against the value: filers state -7 (crores) and
    report to two decimal places of a crore, which XBRL rounding allows.
    """
    _sign, digits, exponent = value.normalize().as_tuple()
    assert isinstance(exponent, int)  # PLAIN_DECIMAL admits no NaN or infinity
    if exponent < -STORED_FRACTION_DIGITS:
        return f"more than {STORED_FRACTION_DIGITS} decimal places"
    if len(digits) + exponent > STORED_INTEGER_DIGITS:
        return f"more than {STORED_INTEGER_DIGITS} integer digits"
    if decimals is None:
        return "no decimals attribute"
    if decimals != "INF" and not re.fullmatch(r"-?\d+", decimals):
        return f"decimals {decimals!r} is neither INF nor an integer"
    return None


def _symbol(meta: dict[str, set[str]], identifiers: set[tuple[str | None, str]]) -> str | None:
    stated = {v for name in mapping.META_SYMBOLS for v in meta.get(name, set()) if v}
    from_entity = {value for scheme, value in identifiers if scheme == "http://www.nseindia.com/NSESymbol" and value}
    symbols = stated | from_entity
    if len(symbols) > 1:
        raise FileRejected(Reason.SHAPE_CHANGED, f"file names more than one symbol: {sorted(symbols)}")
    return next(iter(symbols), None)


def _single(meta: dict[str, set[str]], name: str, *, required: bool = True) -> str | None:
    values = {v for v in meta.get(name, set()) if v}
    if len(values) > 1:
        raise FileRejected(Reason.SHAPE_CHANGED, f"{name} has conflicting values {sorted(values)}")
    if not values:
        if required:
            raise FileRejected(Reason.SHAPE_CHANGED, f"{name} is missing")
        return None
    return next(iter(values))


def _date(text: str | None, what: str) -> date:
    text = (text or "").strip()
    if not ISO_DATE.match(text):
        raise FileRejected(Reason.SHAPE_CHANGED, f"{what}: {text!r} is not a YYYY-MM-DD date")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise FileRejected(Reason.SHAPE_CHANGED, f"{what}: {text!r} is not a valid date") from exc


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _local(tag: str) -> str:
    return _split(tag)[1]
