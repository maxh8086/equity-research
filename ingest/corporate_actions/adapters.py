"""Corporate-action drop-folder adapters (CLAUDE.md store 15, core of Session 3e).

- NseCorporateActionsDrop: NSE's corporate-actions CSV export, downloaded by
  hand. The sidecar's `published_at` is the download time; each row's `as_of`
  is the earlier of that and 00:00 IST on its ex-date (parser.nse_row_as_of).
  Symbols resolve to ISINs through the dated bhavcopy map as it stood the day
  before the ex-date, as known at the row's `as_of`; an unresolvable symbol
  is quarantined, and a rerun retries it once more bhavcopy files are loaded.
- CorporateActionsCuratedDrop: the curated file, one row per action entered
  by a named person from the evidence. Row `as_of` is `announced_at`, its
  `source_url` the evidence URL. A later row with the same `action_key` is a
  new version (a verified ratio, a withdrawal); nothing is edited.

A live NSE adapter waits for Session 7c: until then there is no scraping here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from core.db.models import CorporateAction, CorporateActionQuarantine
from core.db.models import CorporateActionQuarantineReason as Reason
from core.db.pit import corporate_action_file_rows_as_of, raw_source_files_as_of, symbol_to_isin_as_of
from core.timezones import IST
from ingest.base import Adapter, AdapterContext, DropFolderAdapter, RawDocument, RunResult, RunStatus
from ingest.corporate_actions.parser import (
    RULE_VERSION,
    FileRejected,
    ParsedAction,
    ParsedFile,
    RowIssue,
    parse_curated_csv,
    parse_nse_csv,
    terms_problem,
    with_isin,
)

TARGET_STORES = ("corporate_action", "corporate_action_quarantine")

_ACTION_FIELDS = (
    "action_type", "status", "ex_date", "record_date", "face_value_from", "face_value_to",
    "shares_new", "shares_held", "issue_price", "dividend_per_share", "retained_fraction",
    "new_isin", "ratio_basis", "verified_by", "purpose",
)  # fmt: skip


@dataclass
class Tally:
    raw_files: int = 0
    rows_written: int = 0
    quarantined: int = 0
    rejected_files: int = 0
    already_loaded: int = 0
    out_of_scope: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: its format needs a person."""
        detail = (
            f"{self.rows_written} actions, {self.quarantined} quarantined, {self.out_of_scope} out of scope, "
            f"{self.rejected_files} files rejected, {self.already_loaded} rows already loaded"
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


Parse = Callable[[AdapterContext, RawDocument], ParsedFile]


def _resolve_symbols(ctx: AdapterContext, parsed: ParsedFile) -> ParsedFile:
    """NSE rows carry symbols: map each to the ISIN bhavcopy showed the day before its ex-date."""
    actions: list[ParsedAction] = []
    issues: list[RowIssue] = list(parsed.issues)
    unresolved: set[int] = set()
    for action in parsed.actions:
        if action.row_number in unresolved:
            continue
        on = (action.ex_date or action.as_of.astimezone(IST).date()) - timedelta(days=1)
        isin = symbol_to_isin_as_of(ctx.session, symbol=action.symbol, on=on, as_of=action.as_of)
        if isin is None:
            unresolved.add(action.row_number)
            issues.append(
                RowIssue(action.row_number, action.raw_row, Reason.UNKNOWN_SYMBOL,
                         f"no bhavcopy ISIN for {action.symbol} on or before {on}")  # fmt: skip
            )
            continue
        resolved = with_isin(action, isin)
        if problem := terms_problem(resolved):
            unresolved.add(action.row_number)
            issues.append(RowIssue(action.row_number, action.raw_row, Reason.MALFORMED_ROW, problem, isin=isin))
            continue
        actions.append(resolved)
    actions = [a for a in actions if a.row_number not in unresolved]
    return ParsedFile(tuple(actions), tuple(sorted(issues, key=lambda i: i.row_number)), parsed.out_of_scope)


def load_actions(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, parse: Parse, tally: Tally) -> None:
    """Parse one stored file into action versions and quarantine rows, writing only what is new.

    Rows this file already produced under RULE_VERSION are skipped, so a rerun
    or reparse is a no-op except for rows that can now be resolved.
    """
    tally.raw_files += 1
    loaded_actions, loaded_issues = corporate_action_file_rows_as_of(
        ctx.session, content_hash=doc.content_hash, extracted_by=adapter.extracted_by(),
        rule_version=RULE_VERSION, as_of=doc.as_of,
    )  # fmt: skip
    base = dict(content_hash=doc.content_hash, extracted_by=adapter.extracted_by(), model_version=None,
                rule_version=RULE_VERSION)  # fmt: skip
    try:
        parsed = parse(ctx, doc)
    except FileRejected as exc:
        tally.rejected_files += 1
        if (None, str(exc.reason), exc.detail) in loaded_issues:
            tally.already_loaded += 1
            return
        ctx.session.add(
            CorporateActionQuarantine(isin=None, reason=exc.reason, detail=exc.detail, as_of=doc.as_of,
                                      source_url=doc.source_url, **base)  # fmt: skip
        )
        ctx.session.flush()
        tally.quarantined += 1
        return

    tally.out_of_scope += parsed.out_of_scope
    for a in parsed.actions:
        if (a.key, a.as_of) in loaded_actions:
            tally.already_loaded += 1
            continue
        ctx.session.add(
            CorporateAction(
                action_key=a.key, isin=a.isin, source_row=a.row_number, as_of=a.as_of, source_url=a.source_url,
                **{name: getattr(a, name) for name in _ACTION_FIELDS}, **base,
            )  # fmt: skip
        )
        tally.rows_written += 1
    for i in parsed.issues:
        if (i.row_number, str(i.reason), i.detail) in loaded_issues:
            tally.already_loaded += 1
            continue
        ctx.session.add(
            CorporateActionQuarantine(
                isin=i.isin, row_number=i.row_number, raw_row=i.raw_row, reason=i.reason, detail=i.detail,
                as_of=doc.as_of, source_url=doc.source_url, **base,
            )  # fmt: skip
        )
        tally.quarantined += 1
    ctx.session.flush()


def reparse_stored_files(adapter: Adapter, ctx: AdapterContext, parse: Parse) -> RunResult:
    """Re-parse `adapter`'s stored files under the current rule version, from blob storage, never re-fetching."""
    tally = Tally()
    for raw in raw_source_files_as_of(ctx.session, extracted_by=adapter.extracted_by(), as_of=ctx.now()):
        doc = RawDocument(
            data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
            as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
        )  # fmt: skip
        load_actions(adapter, ctx, doc, parse, tally)
    return tally.result(adapter.name)


def _parse_nse(ctx: AdapterContext, doc: RawDocument) -> ParsedFile:
    return _resolve_symbols(ctx, parse_nse_csv(doc.data, file_as_of=doc.as_of, source_url=doc.source_url))


def _parse_curated(ctx: AdapterContext, doc: RawDocument) -> ParsedFile:
    return parse_curated_csv(doc.data, file_as_of=doc.as_of)


class NseCorporateActionsDrop(DropFolderAdapter):
    """NSE corporate-actions CSV exports saved by hand into <EQUITY_DROP_FOLDER>/nse_corporate_actions_drop/.

    Sidecar: `source_url` the export's page, `published_at` the download time.
    """

    name = "nse_corporate_actions_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_actions(self, ctx, doc, _parse_nse, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, meta in self.pending(ctx):
            parse_nse_csv(path.read_bytes(), file_as_of=meta.published_at, source_url=meta.source_url)

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_files(self, ctx, _parse_nse)


class CorporateActionsCuratedDrop(DropFolderAdapter):
    """Curated corporate-action files saved into <EQUITY_DROP_FOLDER>/corporate_actions_curated_drop/.

    Sidecar: `source_url` where the file is kept, `published_at` when it was
    saved. No row may be announced after that.
    """

    name = "corporate_actions_curated_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_actions(self, ctx, doc, _parse_curated, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, meta in self.pending(ctx):
            parse_curated_csv(path.read_bytes(), file_as_of=meta.published_at)

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_files(self, ctx, _parse_curated)
