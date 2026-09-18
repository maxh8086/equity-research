"""XBRL results drop-folder adapter (CLAUDE.md store 1, Session 4).

NseXbrlResultsDrop reads SEBI results XBRL instances downloaded by hand from
NSE's archive (nsearchives.nseindia.com/corporate/xbrl/). The sidecar's
`published_at` is the exchange dissemination time shown on the results page,
never the download time (R2).

- The file's own ISIN is used when it has one (bank and insurer files).
  Otherwise its NSE symbol resolves through the bhavcopy map as it stood in
  the ten days up to publication, and through constituent lists published in
  the 190 days before it. Every ISIN found must agree; a disagreement is
  `isin_conflict`, none at all is `isin_unresolved`, and a rerun retries it
  once more bhavcopy or index files are loaded.
- The publication date must not precede the board meeting, and must follow
  the end of every period reported (`implausible_as_of`).
- One `financial_filing` row per file and rule version; one `financial_facts`
  row per mapped fact, dated at the file's `as_of`; single-fact problems go
  to `financial_facts_quarantine` with the rest of the file kept.

A live NSE listing adapter is not built yet: until then there is no scraping here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from core.compute.isin import is_valid_isin
from core.db.models import FactKind, FinancialFact, FinancialFactsQuarantine, FinancialFiling, XbrlIsinBasis
from core.db.models import FinancialFactsQuarantineReason as Reason
from core.db.pit import (
    financial_filing_loaded_as_of,
    index_symbol_isins_as_of,
    raw_source_files_as_of,
    symbol_to_isin_as_of,
)
from core.timezones import IST
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.nse_xbrl.mapping import RULE_VERSION
from ingest.nse_xbrl.parser import FileRejected, ParsedFiling, parse_filing

TARGET_STORES = ("financial_facts", "financial_filing", "financial_facts_quarantine")

# How far back a symbol -> ISIN read may look. Symbols are reused, so a stale
# row may name another company.
BHAVCOPY_WINDOW = timedelta(days=10)
INDEX_LIST_WINDOW = timedelta(days=190)  # one semi-annual rebalance, plus slack


@dataclass
class Tally:
    raw_files: int = 0
    filings: int = 0
    facts_written: int = 0
    quarantined: int = 0
    rejected_files: int = 0
    already_loaded: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: it needs a person."""
        detail = (
            f"{self.filings} filings, {self.facts_written} facts, {self.quarantined} quarantined, "
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
            rows_written=self.facts_written,
            quarantined=self.quarantined,
        )


def resolve_isin(ctx: AdapterContext, doc: RawDocument, parsed: ParsedFiling) -> tuple[str, XbrlIsinBasis]:
    """The filer's ISIN, from every independent read available at the file's `as_of`. Never guessed."""
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


def _as_of_problem(doc: RawDocument, parsed: ParsedFiling) -> str | None:
    day = doc.as_of.astimezone(IST).date()
    if day < parsed.board_meeting_date:
        return f"published {day}, before the board meeting on {parsed.board_meeting_date}"
    last = max([parsed.period_end, *(f.period_end for f in parsed.facts)])
    if day <= last:
        return f"published {day}, not after the reported period ending {last}"
    return None


def load_filing(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, tally: Tally) -> None:
    """Parse one stored file into a filing, its facts and quarantine rows, unless already done under RULE_VERSION.

    A whole-file quarantine is recorded once per (reason, detail); the file
    is still tried on every run, so an ISIN that becomes resolvable loads.
    """
    tally.raw_files += 1
    parsed_before, rejected_before = financial_filing_loaded_as_of(
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
            FinancialFactsQuarantine(isin=isin, xbrl_element=None, context_ref=None, raw_value=None,
                                     reason=exc.reason, detail=exc.detail, **base)  # fmt: skip
        )
        ctx.session.flush()
        tally.quarantined += 1
        return

    ctx.session.add(
        FinancialFiling(
            isin=isin, isin_basis=basis, symbol=parsed.symbol, scrip_code=parsed.scrip_code,
            consolidation=parsed.consolidation, taxonomy=parsed.taxonomy.family,
            reporting_quarter=parsed.reporting_quarter, period_start=parsed.period_start,
            period_end=parsed.period_end, board_meeting_date=parsed.board_meeting_date,
            facts_written=len(parsed.facts), facts_quarantined=len(parsed.issues),
            dimensional_facts_deferred=parsed.dimensional_facts_deferred, **base,
        )  # fmt: skip
    )
    for f in parsed.facts:
        ctx.session.add(
            FinancialFact(
                isin=isin, consolidation=parsed.consolidation, fact_kind=FactKind.REPORTED,
                line_item=f.line_item, xbrl_element=f.xbrl_element, period_start=f.period_start,
                period_end=f.period_end, value=f.value, unit=f.unit, **base,
            )  # fmt: skip
        )
    for i in parsed.issues:
        ctx.session.add(
            FinancialFactsQuarantine(isin=isin, xbrl_element=i.xbrl_element, context_ref=i.context_ref,
                                     raw_value=i.raw_value, reason=i.reason, detail=i.detail, **base)  # fmt: skip
        )
    ctx.session.flush()
    tally.filings += 1
    tally.facts_written += len(parsed.facts)
    tally.quarantined += len(parsed.issues)


class NseXbrlResultsDrop(DropFolderAdapter):
    """Results XBRL files saved by hand into <EQUITY_DROP_FOLDER>/nse_xbrl_results_drop/.

    Sidecar: `source_url` the nsearchives URL, `published_at` the exchange
    dissemination time (IST), `media_type` "application/xml".
    """

    name = "nse_xbrl_results_drop"
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
