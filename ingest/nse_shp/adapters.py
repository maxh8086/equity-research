"""Shareholding-pattern drop-folder adapter (CLAUDE.md store 16, Session 4c).

NseShpDrop reads SEBI Regulation 31 shareholding-pattern XBRL instances
downloaded by hand from NSE's archive (nsearchives.nseindia.com/corporate/xbrl/).
The sidecar's `published_at` is the exchange broadcast time shown on the
shareholding-pattern page, never the download time (R2).

- ISIN: the file's own ISIN, checked against the bhavcopy map as it stood in
  the ten days up to publication and against constituent lists published in
  the 190 days before it. Every ISIN found must agree; a disagreement is
  `isin_conflict`, none at all is `isin_unresolved`.
- `as_of` must fall after the "as on" date of the pattern, and not before the
  time NSE's file name says the file was generated (`implausible_as_of`).
- One `shareholding_filing` row per file and rule version; one
  `shareholding_pattern` row per (category, measure) count, dated at the file's
  `as_of`. A revised filing is another file with a later `as_of`; reads pick
  the newest filing known at `t` for each "as on" date.

A live NSE listing adapter is not built yet: until then there is no scraping here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from posixpath import basename
from urllib.parse import urlparse

from core.compute.isin import is_valid_isin
from core.db.models import ShareholdingFiling, ShareholdingPattern, ShareholdingQuarantine, XbrlIsinBasis
from core.db.models import ShareholdingQuarantineReason as Reason
from core.db.pit import (
    index_symbol_isins_as_of,
    raw_source_files_as_of,
    shareholding_filing_loaded_as_of,
    symbol_to_isin_as_of,
)
from core.timezones import IST
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.nse_shp.mapping import RULE_VERSION
from ingest.nse_shp.parser import FileRejected, ParsedFiling, parse_filing

TARGET_STORES = ("shareholding_pattern", "shareholding_filing", "shareholding_quarantine")

# How far back a symbol -> ISIN read may look. Symbols are reused, so a stale
# row may name another company.
BHAVCOPY_WINDOW = timedelta(days=10)
INDEX_LIST_WINDOW = timedelta(days=190)  # one semi-annual rebalance, plus slack

# NSE names each file SHP_<id>[_<id>]_<DDMMYYYYhhmmss>_WEB.xml, with a 12-hour
# clock and no AM/PM marker.
GENERATED_AT = re.compile(r"_(\d{2})(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})_WEB\.xml$")


@dataclass
class Tally:
    raw_files: int = 0
    filings: int = 0
    rows_written: int = 0
    quarantined: int = 0
    rejected_files: int = 0
    already_loaded: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: it needs a person."""
        detail = (
            f"{self.filings} filings, {self.rows_written} rows, {self.quarantined} quarantined, "
            f"{self.rejected_files} files rejected, {self.already_loaded} files already loaded"
        )
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        failed = bool(self.rejected_files or self.problems)
        return RunResult(
            adapter,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.rows_written,
            quarantined=self.quarantined,
        )


def resolve_isin(ctx: AdapterContext, doc: RawDocument, parsed: ParsedFiling) -> tuple[str, XbrlIsinBasis]:
    """The filer's ISIN, from every independent read available at the file's `as_of`. Never guessed.

    A copy of ingest/nse_xbrl's resolver: ingest packages do not import each other.
    """
    found: dict[XbrlIsinBasis, set[str]] = {}
    if parsed.isin is not None:
        if not is_valid_isin(parsed.isin):
            raise FileRejected(Reason.ISIN_CONFLICT, f"ISIN in the file {parsed.isin!r} is not a valid ISIN")
        found[XbrlIsinBasis.FILING] = {parsed.isin}
    if parsed.symbol is not None:
        day = doc.as_of.astimezone(IST).date()
        bhav = symbol_to_isin_as_of(
            ctx.session, symbol=parsed.symbol, on=day, since=day - BHAVCOPY_WINDOW, as_of=doc.as_of
        )
        if bhav is not None:
            found[XbrlIsinBasis.BHAVCOPY] = {bhav}
        listed = index_symbol_isins_as_of(
            ctx.session, symbol=parsed.symbol, since=doc.as_of - INDEX_LIST_WINDOW, as_of=doc.as_of
        )
        if listed:
            found[XbrlIsinBasis.INDEX_LIST] = listed
    isins = set().union(*found.values())
    if not isins:
        raise FileRejected(
            Reason.ISIN_UNRESOLVED,
            f"no ISIN in the file, and no bhavcopy or index-list ISIN for symbol {parsed.symbol!r}",
        )
    if len(isins) > 1:
        detail = ", ".join(f"{basis.value}: {sorted(v)}" for basis, v in found.items())
        raise FileRejected(Reason.ISIN_CONFLICT, f"ISIN reads disagree ({detail})")
    basis = next(b for b in XbrlIsinBasis if b in found)  # declaration order: filing first
    return isins.pop(), basis


def generated_no_earlier_than(source_url: str) -> datetime | None:
    """The earliest time NSE's file name allows: its clock read as AM. None if the name has no timestamp."""
    match = GENERATED_AT.search(basename(urlparse(source_url).path))
    if match is None:
        return None
    day, month, year, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour % 12 if hour <= 12 else hour, minute, second, tzinfo=IST)
    except ValueError:
        return None


def _as_of_problem(doc: RawDocument, parsed: ParsedFiling) -> str | None:
    day = doc.as_of.astimezone(IST).date()
    if day <= parsed.as_on_date:
        return f"published {day}, not after the pattern's as-on date {parsed.as_on_date}"
    generated = generated_no_earlier_than(doc.source_url)
    if generated is not None and doc.as_of < generated:
        return f"published {doc.as_of.astimezone(IST)}, before the file name's timestamp {generated}"
    return None


def load_filing(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    """Parse one stored file into a filing, its counts and quarantine rows, unless already done under RULE_VERSION.

    A whole-file quarantine is recorded once per (reason, detail); the file
    is still tried on every run, so an ISIN that becomes resolvable loads.
    """
    tally.raw_files += 1
    parsed_before, rejected_before = shareholding_filing_loaded_as_of(
        ctx.session, content_hash=doc.content_hash, file_as_of=doc.as_of, rule_version=RULE_VERSION,
        as_of=doc.as_of,
    )  # fmt: skip
    if parsed_before:
        tally.already_loaded += 1
        return
    base = dict(as_of=doc.as_of, content_hash=doc.content_hash, source_url=doc.source_url,
                extracted_by=adapter.extracted_by(), model_version=None, rule_version=RULE_VERSION)  # fmt: skip
    isin: str | None = None
    try:
        parsed = parse_filing(doc.data)
        if problem := _as_of_problem(doc, parsed):
            raise FileRejected(Reason.IMPLAUSIBLE_AS_OF, problem)
        isin, basis = resolve_isin(ctx, doc, parsed)
    except FileRejected as exc:
        tally.rejected_files += 1
        if (str(exc.reason), exc.detail) in rejected_before:
            tally.already_loaded += 1
            return
        ctx.session.add(
            ShareholdingQuarantine(isin=isin, xbrl_element=None, context_ref=None, raw_value=None,
                                   reason=exc.reason, detail=exc.detail, **base)  # fmt: skip
        )
        ctx.session.flush()
        tally.quarantined += 1
        return

    ctx.session.add(
        ShareholdingFiling(
            isin=isin, isin_basis=basis, symbol=parsed.symbol, scrip_code=parsed.scrip_code,
            as_on_date=parsed.as_on_date, allotment_date=parsed.allotment_date,
            taxonomy_version=parsed.taxonomy_version, rows_written=len(parsed.facts),
            facts_quarantined=len(parsed.issues), typed_facts_deferred=parsed.typed_facts_deferred,
            percentage_facts_skipped=parsed.percentage_facts_skipped, **base,
        )  # fmt: skip
    )
    for f in parsed.facts:
        ctx.session.add(
            ShareholdingPattern(
                isin=isin, as_on_date=parsed.as_on_date, category=f.category, parent_category=f.parent_category,
                measure=f.measure, value=f.value, xbrl_element=f.xbrl_element, xbrl_member=f.xbrl_member, **base,
            )  # fmt: skip
        )
    for i in parsed.issues:
        ctx.session.add(
            ShareholdingQuarantine(isin=isin, xbrl_element=i.xbrl_element, context_ref=i.context_ref,
                                   raw_value=i.raw_value, reason=i.reason, detail=i.detail, **base)  # fmt: skip
        )
    ctx.session.flush()
    tally.filings += 1
    tally.rows_written += len(parsed.facts)
    tally.quarantined += len(parsed.issues)


class NseShpDrop(DropFolderAdapter):
    """Shareholding-pattern XBRL files saved by hand into <EQUITY_DROP_FOLDER>/nse_shareholding_drop/.

    Sidecar: `source_url` the nsearchives URL (its file name carries NSE's
    timestamp), `published_at` the exchange broadcast time (IST),
    `media_type` "application/xml".
    """

    name = "nse_shareholding_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_filing(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _ in self.pending(ctx):
            parse_filing(path.read_bytes())

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        """Re-parse every stored file under the current RULE_VERSION, from blob storage, never re-fetching."""
        tally = Tally()
        for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=ctx.now()):
            doc = RawDocument(
                data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
                as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
            )  # fmt: skip
            load_filing(self, ctx, doc, tally)
        return tally.result(self.name)
