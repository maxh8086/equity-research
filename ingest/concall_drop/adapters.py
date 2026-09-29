"""Concall-transcript drop-folder adapter (CLAUDE.md store 2, Session 9).

ConcallTranscriptDrop reads earnings-call transcripts downloaded by hand --
from the company's investor-relations page or the exchange announcement that
carried them -- and turns each into text, an ISIN and a call date. No model
runs here and none is needed: everything this adapter decides, it decides by
code (R1).

- Symbol and call date come from the drop file's name,
  `<SYMBOL>_<YYYY-MM-DD>[_<note>].pdf`. Nothing inside a transcript states
  them in a form a parser can trust.
- ISIN: the symbol is resolved against the bhavcopy map as it stood in the ten
  days up to publication and against constituent lists published in the 190
  days before it. Every ISIN found must agree; a disagreement is
  `isin_conflict`, none at all is `isin_unresolved`.
- `as_of` is the sidecar's `published_at`: when the transcript was put up, not
  when it was downloaded (R2). It must not precede the call itself.

A live adapter that follows exchange announcements to transcript PDFs is not
built yet; until then there is no scraping here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from core.db.models import ConcallDocument, GuidanceQuarantine, XbrlIsinBasis
from core.db.models import GuidanceIssueReason as Reason
from core.db.pit import (
    concall_document_loaded_as_of,
    index_symbol_isins_as_of,
    raw_source_files_as_of,
    symbol_to_isin_as_of,
)
from core.timezones import IST
from ingest.base import (
    Adapter,
    AdapterContext,
    DropFileError,
    DropFileMeta,
    DropFolderAdapter,
    RawDocument,
    RunResult,
    RunStatus,
)
from ingest.concall_drop.parser import RULE_VERSION, DocumentRejected, TranscriptDocument, parse_transcript

TARGET_STORES = ("raw_source_file", "concall_document", "guidance_quarantine")

# Same windows as ingest/nse_shp: symbols are reused, so a stale row may name
# another company.
BHAVCOPY_WINDOW = timedelta(days=10)
INDEX_LIST_WINDOW = timedelta(days=190)


@dataclass
class Tally:
    raw_files: int = 0
    documents: int = 0
    rejected_files: int = 0
    quarantined: int = 0
    already_loaded: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: it needs a person."""
        detail = (
            f"{self.documents} transcripts, {self.rejected_files} files rejected, "
            f"{self.quarantined} quarantined, {self.already_loaded} files already loaded"
        )
        if self.problems:
            detail += "; " + "; ".join(self.problems)
        failed = bool(self.rejected_files or self.problems)
        return RunResult(
            adapter,
            RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            detail,
            raw_files=self.raw_files,
            rows_written=self.documents,
            quarantined=self.quarantined,
        )


def resolve_isin(ctx: AdapterContext, doc: RawDocument, symbol: str) -> tuple[str, XbrlIsinBasis]:
    """The company's ISIN, from every dated read available at the file's `as_of`. Never guessed.

    A transcript carries no ISIN of its own, so unlike the XBRL resolvers this
    one has only the symbol to go on and both reads must agree.
    """
    found: dict[XbrlIsinBasis, set[str]] = {}
    day = doc.as_of.astimezone(IST).date()
    bhav = symbol_to_isin_as_of(
        ctx.session, symbol=symbol, on=day, since=day - BHAVCOPY_WINDOW, as_of=doc.as_of
    )
    if bhav is not None:
        found[XbrlIsinBasis.BHAVCOPY] = {bhav}
    listed = index_symbol_isins_as_of(
        ctx.session, symbol=symbol, since=doc.as_of - INDEX_LIST_WINDOW, as_of=doc.as_of
    )
    if listed:
        found[XbrlIsinBasis.INDEX_LIST] = listed
    isins = set().union(*found.values()) if found else set()
    if not isins:
        raise DocumentRejected(
            Reason.ISIN_UNRESOLVED, f"no bhavcopy or index-list ISIN for symbol {symbol!r} at {day}"
        )
    if len(isins) > 1:
        detail = ", ".join(f"{basis.value}: {sorted(v)}" for basis, v in found.items())
        raise DocumentRejected(Reason.ISIN_CONFLICT, f"ISIN reads disagree ({detail})")
    basis = next(b for b in XbrlIsinBasis if b in found)
    return isins.pop(), basis


def _as_of_problem(doc: RawDocument, parsed: TranscriptDocument) -> str | None:
    day = doc.as_of.astimezone(IST).date()
    if day < parsed.call_date:
        return f"published {day}, before the call on {parsed.call_date}"
    return None


def load_document(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, filename: str, tally: Tally) -> None:
    """Turn one stored file into a concall_document row, unless that was already done under RULE_VERSION.

    A rejection is recorded once per (reason, detail); the file is still tried
    on every run, so a symbol that becomes resolvable once its bhavcopy is
    loaded goes through without anyone touching the drop folder.
    """
    tally.raw_files += 1
    parsed_before, rejected_before = concall_document_loaded_as_of(
        ctx.session, content_hash=doc.content_hash, file_as_of=doc.as_of, rule_version=RULE_VERSION,
        as_of=doc.as_of,
    )  # fmt: skip
    if parsed_before:
        tally.already_loaded += 1
        return
    base = dict(as_of=doc.as_of, content_hash=doc.content_hash, source_url=doc.source_url,
                extracted_by=adapter.extracted_by(), model_version=None, rule_version=RULE_VERSION)  # fmt: skip
    try:
        parsed = parse_transcript(doc.data, filename=filename, media_type=doc.media_type)
        if problem := _as_of_problem(doc, parsed):
            raise DocumentRejected(Reason.IMPLAUSIBLE_AS_OF, problem)
        isin, basis = resolve_isin(ctx, doc, parsed.symbol)
    except DocumentRejected as exc:
        tally.rejected_files += 1
        if (str(exc.reason), exc.detail) in rejected_before:
            tally.already_loaded += 1
            return
        ctx.session.add(
            GuidanceQuarantine(isin=None, quote=None, reason=exc.reason, detail=exc.detail,
                               prompt_hash=None, extractor_version=None, **base)  # fmt: skip
        )
        ctx.session.flush()
        tally.quarantined += 1
        return

    ctx.session.add(
        ConcallDocument(
            isin=isin, isin_basis=basis, symbol=parsed.symbol, call_date=parsed.call_date,
            page_count=parsed.page_count, char_count=len(parsed.text), **base,
        )  # fmt: skip
    )
    ctx.session.flush()
    tally.documents += 1


def drop_filename(source_url: str) -> str:
    """The conventional name a stored file was read under.

    `store_raw` keeps the sidecar's `source_url`, not the local path, so a
    reparse needs the name back. The convention is that the drop file is named
    after the last segment of its source URL wherever that already follows
    `<SYMBOL>_<YYYY-MM-DD>`; where it does not, the person renames the file and
    records the real URL in the sidecar, and `#name=` on the URL carries the
    name they used.
    """
    url, _, fragment = source_url.partition("#")
    if fragment.startswith("name="):
        return fragment.removeprefix("name=")
    return url.rsplit("/", 1)[-1].partition("?")[0]


class ConcallTranscriptDrop(DropFolderAdapter):
    """Transcripts saved by hand into <EQUITY_DROP_FOLDER>/concall_transcript_drop/.

    File name: `<SYMBOL>_<YYYY-MM-DD>[_<note>].pdf` or `.txt`, where the date
    is the call date. Sidecar: `source_url` the page or file it came from --
    with `#name=<the file name used>` appended when the URL's own last segment
    is not that name -- `published_at` the time the transcript was put up
    (IST), `media_type` "application/pdf" or "text/plain".
    """

    name = "concall_transcript_drop"
    target_stores = TARGET_STORES

    def named_files(self, ctx: AdapterContext) -> list[tuple[Path, DropFileMeta]]:
        """Pending files whose name a reparse will recover from the sidecar's URL.

        Checked at ingest rather than discovered at reparse: a file stored
        under a name `drop_filename` cannot give back would parse fine today
        and be rejected the day someone fixes the parser.
        """
        files = self.pending(ctx)
        if wrong := [p.name for p, meta in files if drop_filename(meta.source_url) != p.name]:
            raise DropFileError(
                f"sidecar source_url does not end in the file's own name; add #name=<file> to it: {wrong}"
            )
        return files

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for path, meta in self.named_files(ctx):
            doc = self.store_raw(
                ctx, data=path.read_bytes(), source_url=meta.source_url, as_of=meta.published_at,
                media_type=meta.media_type,
            )  # fmt: skip
            load_document(self, ctx, doc, path.name, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, meta in self.named_files(ctx):
            parse_transcript(path.read_bytes(), filename=path.name, media_type=meta.media_type)

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        """Re-read every stored file under the current RULE_VERSION, from blob storage, never re-fetching."""
        tally = Tally()
        for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=ctx.now()):
            doc = RawDocument(
                data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
                as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
            )  # fmt: skip
            load_document(self, ctx, doc, drop_filename(raw.source_url), tally)
        return tally.result(self.name)
