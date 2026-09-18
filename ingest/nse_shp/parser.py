"""Parse one SEBI shareholding-pattern XBRL instance into counts. Pure: no I/O, no DB.

File shape (checked against the contract fixtures):
- Counts sit in contexts carrying exactly one explicit member on
  `CategoryOfShareholdersAxis`, dated at an instant equal to `DateOfReport`
  (the "as on" date of the pattern).
- Contexts with no dimension are named `Main[P...Y](D|I)`. They hold the
  identity facts (ISIN, symbol, scrip code, dates) and, in the 2025 layouts,
  the foreign-investment limit percentages for this and past quarters.
- Contexts carrying a typed member list named holders (above 1%, significant
  beneficial owners). Their numeric facts are counted and deferred.
- The 2020 layout reports its identity facts under context ids no context
  defines (`OneD`, `OneI`); tolerated for text facts only.
- Counts are whole numbers with decimals="INF".

Every parent category must equal the sum of its children, for every measure
(mapping.CATEGORIES); a file that fails this is rejected whole.

Whole-file problems raise FileRejected; single-fact problems become FactIssue
entries and the rest of the file is kept. Nothing is guessed.
"""

import io
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from posixpath import basename

from core.db.models import ShareholdingQuarantineReason as Reason
from ingest.nse_shp import mapping
from ingest.nse_shp.mapping import Layout

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"
LINK = "{http://www.xbrl.org/2003/linkbase}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

PLAIN_CONTEXT = re.compile(r"^Main(P*Y)?[DI]$")
WHOLE_NUMBER = re.compile(r"^\d{1,18}$")  # shareholding_pattern.value is BIGINT
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SCHEMA_REF = re.compile(r"^in-bse-shp-(\d{4}-\d{2}-\d{2})\.xsd$")

SCRIP_CODE_SCHEMES = frozenset(
    {"http://www.bseindia.com/in-bse-shp/ScripCode", "http://www.bseindia.com/bse-shp/ScripCode"}
)
NSE_SYMBOL_SCHEME = "http://www.nseindia.com/NSESymbol"
PREFIX = "in-bse-shp"


class FileRejected(Exception):
    def __init__(self, reason: Reason, detail: str):
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ParsedValue:
    category: str
    parent_category: str | None
    measure: str
    value: int
    xbrl_element: str  # "in-bse-shp:<local name>"
    xbrl_member: str  # "in-bse-shp:<member local name>"
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
    taxonomy_version: str
    layout: Layout
    symbol: str | None
    isin: str | None
    scrip_code: str | None
    as_on_date: date
    allotment_date: date | None
    facts: tuple[ParsedValue, ...]
    issues: tuple[FactIssue, ...]
    typed_facts_deferred: int
    percentage_facts_skipped: int


@dataclass(frozen=True)
class _Context:
    kind: str  # "plain", "category" or "typed"
    member: str | None  # member local name, for a category context
    instant: date | None


def parse_filing(data: bytes) -> ParsedFiling:
    root, namespaces = _load(data)
    version, layout = _detect_taxonomy(root, namespaces)
    namespace = mapping.NAMESPACE.format(version=version)
    contexts = _contexts(root, namespaces, namespace)
    units = _units(root, namespaces)

    meta: dict[str, set[str]] = defaultdict(set)
    category_facts: list[ET.Element] = []
    plain_issues: list[FactIssue] = []
    typed = 0
    percentages = 0

    for el in root:
        ref = el.get("contextRef")
        if ref is None:
            continue
        el_namespace, local = _split(el.tag)
        numeric = el.get("unitRef") is not None
        ctx = contexts.get(ref)
        if ctx is None:
            if numeric or ref not in mapping.UNDEFINED_IDENTITY_CONTEXTS[layout]:
                raise FileRejected(Reason.SHAPE_CHANGED, f"fact {local} refers to unknown context {ref!r}")
            if el.get(XSI_NIL) != "true":
                meta[local].add((el.text or "").strip())
            continue
        if ctx.kind == "typed":
            typed += numeric
            continue
        if ctx.kind == "category":
            if not numeric:
                raise FileRejected(Reason.SHAPE_CHANGED, f"text fact {local} in category context {ref!r}")
            category_facts.append(el)
            continue
        # plain context
        if el.get(XSI_NIL) == "true":
            continue
        if not numeric:
            meta[local].add((el.text or "").strip())
        elif el_namespace == namespace and local in mapping.PERCENTAGES[layout]:
            percentages += 1
        else:
            plain_issues.append(
                FactIssue(
                    _qualified(el.tag, namespace), ref, (el.text or "").strip(), Reason.UNMAPPED_ELEMENT,
                    "numeric fact in a context with no category",
                )  # fmt: skip
            )

    as_on_date = _date(_single(meta, mapping.META_DATE_OF_REPORT), mapping.META_DATE_OF_REPORT)
    allotment = _single(meta, mapping.META_DATE_OF_ALLOTMENT, required=False)
    for cid, ctx in contexts.items():
        if ctx.kind == "category" and ctx.instant != as_on_date:
            raise FileRejected(
                Reason.SHAPE_CHANGED, f"category context {cid!r} is dated {ctx.instant}, not {as_on_date}"
            )

    symbol = _single(meta, mapping.META_SYMBOL, required=False)
    scrip_code = _single(meta, mapping.META_SCRIP_CODE, required=False)
    _check_identifiers(root, symbol, scrip_code)

    facts, issues, skipped = _classify(category_facts, layout, namespace, contexts, units)
    _check_totals(facts, issues, layout)
    for category in mapping.REQUIRED_CATEGORIES:
        if not any(f.category == category and f.measure == "total_shares" for f in facts):
            raise FileRejected(Reason.SHAPE_CHANGED, f"no total_shares reported for {category}")

    return ParsedFiling(
        taxonomy_version=version,
        layout=layout,
        symbol=symbol,
        isin=_single(meta, mapping.META_ISIN, required=False),
        scrip_code=scrip_code,
        as_on_date=as_on_date,
        allotment_date=_date(allotment, mapping.META_DATE_OF_ALLOTMENT) if allotment is not None else None,
        facts=facts,
        issues=tuple(plain_issues) + issues,
        typed_facts_deferred=typed,
        percentage_facts_skipped=percentages + skipped,
    )


def _load(data: bytes) -> tuple[ET.Element, dict[str, str]]:
    # Stdlib ElementTree expands internal entities; shareholding filings never declare any.
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


def _detect_taxonomy(root: ET.Element, namespaces: dict[str, str]) -> tuple[str, Layout]:
    refs = [basename(ref.get(XLINK_HREF) or "") for ref in root.iter(f"{LINK}schemaRef")]
    if len(refs) != 1:
        raise FileRejected(Reason.SHAPE_CHANGED, f"expected one schemaRef, found {refs}")
    match = SCHEMA_REF.match(refs[0])
    version = match.group(1) if match else None
    if (
        version is None
        or version not in mapping.TAXONOMY_VERSIONS
        or mapping.NAMESPACE.format(version=version) not in namespaces.values()
    ):
        raise FileRejected(Reason.UNSUPPORTED_TAXONOMY, f"schemaRef {refs[0]!r} with no supported entry point")
    return version, mapping.TAXONOMY_VERSIONS[version]


def _contexts(root: ET.Element, namespaces: dict[str, str], namespace: str) -> dict[str, _Context]:
    contexts = {}
    for ctx in root.iter(f"{XBRLI}context"):
        cid = ctx.get("id") or ""
        period = ctx.find(f"{XBRLI}period")
        instant = period.find(f"{XBRLI}instant") if period is not None else None
        end = period.find(f"{XBRLI}endDate") if period is not None else None
        if instant is None and end is None:
            raise FileRejected(Reason.SHAPE_CHANGED, f"context {cid!r} has no period")
        explicit = list(ctx.iter(f"{XBRLDI}explicitMember"))
        has_typed = next(ctx.iter(f"{XBRLDI}typedMember"), None) is not None
        instant_date = _date(instant.text, cid) if instant is not None else None
        if has_typed:
            contexts[cid] = _Context("typed", None, instant_date)
        elif not explicit:
            if not PLAIN_CONTEXT.match(cid):
                raise FileRejected(Reason.SHAPE_CHANGED, f"unexpected context id {cid!r} with no dimension")
            contexts[cid] = _Context("plain", None, instant_date)
        else:
            if len(explicit) != 1:
                raise FileRejected(Reason.SHAPE_CHANGED, f"context {cid!r} has {len(explicit)} explicit members")
            dimension = _resolve(explicit[0].get("dimension") or "", namespaces)
            member = _resolve((explicit[0].text or "").strip(), namespaces)
            if dimension != (namespace, mapping.CATEGORY_AXIS) or member[0] != namespace:
                raise FileRejected(Reason.SHAPE_CHANGED, f"context {cid!r} uses an unexpected dimension or member")
            if instant_date is None:
                raise FileRejected(Reason.SHAPE_CHANGED, f"category context {cid!r} is not an instant")
            contexts[cid] = _Context("category", member[1], instant_date)
    return contexts


def _units(root: ET.Element, namespaces: dict[str, str]) -> dict[str, str | None]:
    """unit id -> our unit name, or None for a unit we do not recognise (divides included)."""
    units = {}
    for unit in root.iter(f"{XBRLI}unit"):
        if unit.find(f"{XBRLI}divide") is not None:
            units[unit.get("id") or ""] = None
            continue
        key = tuple(
            sorted(_resolve((m.text or "").strip(), namespaces) for m in unit.findall(f"{XBRLI}measure"))
        )
        units[unit.get("id") or ""] = mapping.UNITS.get(key)
    return units


def _classify(
    elements: list[ET.Element],
    layout: Layout,
    namespace: str,
    contexts: dict[str, _Context],
    units: dict[str, str | None],
) -> tuple[tuple[ParsedValue, ...], tuple[FactIssue, ...], int]:
    issues: list[FactIssue] = []
    candidates: dict[tuple[str, str], list[tuple[ParsedValue, str]]] = defaultdict(list)
    categories = mapping.CATEGORIES[layout]
    measures = mapping.MEASURES[layout]
    skipped = 0

    for el in elements:
        ref = el.get("contextRef") or ""
        member = contexts[ref].member or ""
        el_namespace, local = _split(el.tag)
        element = _qualified(el.tag, namespace)
        raw = (el.text or "").strip()

        def issue(reason: Reason, detail: str) -> None:
            issues.append(FactIssue(element, ref, raw, reason, detail))

        if el.get(XSI_NIL) == "true":
            continue
        category = categories.get(member)
        if category is None:
            issue(Reason.UNMAPPED_CATEGORY, f"member {member} not in the mapping table")
            continue
        if el_namespace == namespace and local in mapping.PERCENTAGES[layout]:
            skipped += 1
            continue
        measure = measures.get(local) if el_namespace == namespace else None
        if measure is None:
            issue(Reason.UNMAPPED_ELEMENT, "element not in the mapping table")
            continue
        unit = units.get(el.get("unitRef") or "")
        if unit != measure.unit:
            issue(Reason.UNEXPECTED_UNIT, f"unit {el.get('unitRef')!r} resolves to {unit}, expected {measure.unit}")
            continue
        if el.get("decimals") != "INF":
            issue(Reason.MALFORMED_VALUE, f"decimals {el.get('decimals')!r}, expected INF for a count")
            continue
        if not WHOLE_NUMBER.match(raw):
            issue(Reason.MALFORMED_VALUE, "not a whole number of at most 18 digits")
            continue
        value = ParsedValue(
            category=category.key,
            parent_category=category.parent,
            measure=measure.key,
            value=int(raw),
            xbrl_element=element,
            xbrl_member=f"{PREFIX}:{member}",
            context_refs=(ref,),
        )
        candidates[(category.key, measure.key)].append((value, raw))

    facts = []
    for group in candidates.values():
        values = {v.value for v, _raw in group}
        if len(values) == 1:
            first = group[0][0]
            refs = tuple(sorted({ref for v, _raw in group for ref in v.context_refs}))
            facts.append(
                ParsedValue(
                    first.category, first.parent_category, first.measure, first.value,
                    first.xbrl_element, first.xbrl_member, refs,
                )  # fmt: skip
            )
            continue
        for v, raw in group:
            issues.append(
                FactIssue(
                    v.xbrl_element, v.context_refs[0], raw, Reason.CONFLICTING_VALUES,
                    f"{v.category}: reported {len(group)} times with values {sorted(values)}",
                )  # fmt: skip
            )
    facts.sort(key=lambda f: (f.category, f.measure))
    return tuple(facts), tuple(issues), skipped


def _check_totals(facts: tuple[ParsedValue, ...], issues: tuple[FactIssue, ...], layout: Layout) -> None:
    """Every parent equals the sum of its children, for each measure (absent counts as zero).

    A measure with any fact-level issue is not checked: a quarantined value
    would make the sum wrong for a reason already recorded.
    """
    measures_by_element = {f"{PREFIX}:{local}": m.key for local, m in mapping.MEASURES[layout].items()}
    doubtful = {measures_by_element.get(i.xbrl_element) for i in issues}
    values = {(f.category, f.measure): f.value for f in facts}
    children: dict[str, list[str]] = defaultdict(list)
    for category in mapping.CATEGORIES[layout].values():
        if category.parent is not None:
            children[category.parent].append(category.key)
    for measure in sorted({f.measure for f in facts} - doubtful):
        for parent, kids in sorted(children.items()):
            reported = [(parent, measure) in values] + [(k, measure) in values for k in kids]
            if not any(reported):
                continue
            total = values.get((parent, measure), 0)
            parts = sum(values.get((k, measure), 0) for k in kids)
            if total != parts:
                raise FileRejected(
                    Reason.TOTALS_MISMATCH, f"{parent} {measure} is {total}, but its categories sum to {parts}"
                )


def _check_identifiers(root: ET.Element, symbol: str | None, scrip_code: str | None) -> None:
    for ident in root.iter(f"{XBRLI}identifier"):
        scheme, value = ident.get("scheme"), (ident.text or "").strip()
        if scheme in SCRIP_CODE_SCHEMES:
            expected, what = scrip_code, mapping.META_SCRIP_CODE
        elif scheme == NSE_SYMBOL_SCHEME:
            expected, what = symbol, mapping.META_SYMBOL
        else:
            raise FileRejected(Reason.SHAPE_CHANGED, f"unknown entity identifier scheme {scheme!r}")
        if value != expected:
            raise FileRejected(Reason.SHAPE_CHANGED, f"entity identifier {value!r} does not match {what} {expected!r}")


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


def _resolve(qname: str, namespaces: dict[str, str]) -> tuple[str, str]:
    prefix, _, local = qname.rpartition(":")
    return namespaces.get(prefix, prefix), local


def _qualified(tag: str, namespace: str) -> str:
    el_namespace, local = _split(tag)
    return f"{PREFIX}:{local}" if el_namespace == namespace else tag


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag
