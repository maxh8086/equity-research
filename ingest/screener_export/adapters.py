"""Screener.in export drop-folder adapter (Session 6).

ScreenerExportDrop reads Excel files exported by hand from a Screener.in
company page. They are the outside reference the validation compares our
computed numbers with; nothing in the knowledge stores is ever derived from
them.

- The sidecar names the ISIN and consolidation, because the workbook carries
  only a company name. The ISIN is never guessed: the Screener URL's symbol
  must map to that ISIN in the constituent lists published in the 190 days up
  to the export, and the ISIN must be in the universe. Otherwise the file is
  rejected and the run fails.
- `published_at` is the export (download) time: Screener builds the file on
  request, so that is when these numbers were public. Every period in the
  sheet must end before it (R2).
- One `screener_export` row per file and rule version, one `screener_value`
  row per non-blank cell. Screener revises its numbers: a later export is a
  new file with a later `as_of`, and reads pick the newest one known at `t`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from core.db.models import Consolidation, ScreenerExport, ScreenerValue
from core.db.pit import (
    index_symbol_isins_as_of,
    index_universe_isins_as_of,
    raw_source_files_as_of,
    screener_export_loaded_as_of,
    screener_exports_as_of,
)
from core.timezones import IST
from ingest.base import (
    Adapter,
    AdapterContext,
    DropFileMeta,
    DropFolderAdapter,
    RawDocument,
    RunResult,
    RunStatus,
)
from ingest.screener_export.parser import RULE_VERSION, ExportRejected, parse_export

TARGET_STORES = ("screener_export", "screener_value")

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
INDEX_LIST_WINDOW = timedelta(days=190)  # one semi-annual rebalance, plus slack

# https://www.screener.in/company/<NSE symbol>/[consolidated/]
SCREENER_URL = re.compile(r"^https://www\.screener\.in/company/([A-Z0-9&-]+)/(consolidated/)?$")


class ScreenerExportMeta(DropFileMeta):
    """Sidecar for a Screener export: provenance plus the company it belongs to."""

    isin: str
    consolidation: Consolidation


@dataclass
class Tally:
    raw_files: int = 0
    exports: int = 0
    rows_written: int = 0
    rejected_files: int = 0
    already_loaded: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if a file was rejected: it needs a person."""
        detail = (
            f"{self.exports} exports, {self.rows_written} values, "
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
        )


def check_identity(ctx: AdapterContext, doc: RawDocument, isin: str, consolidation: Consolidation) -> None:
    """The sidecar's company must be the company the Screener URL names, as known at the export time."""
    match = SCREENER_URL.match(doc.source_url)
    if match is None:
        raise ExportRejected(f"source_url {doc.source_url!r} is not a Screener company page URL")
    symbol, consolidated = match.group(1), match.group(2) is not None
    if consolidated != (consolidation is Consolidation.CONSOLIDATED):
        raise ExportRejected(f"source_url {doc.source_url!r} does not match consolidation {consolidation.value!r}")
    listed = index_symbol_isins_as_of(
        ctx.session, symbol=symbol, since=doc.as_of - INDEX_LIST_WINDOW, as_of=doc.as_of
    )
    if listed != {isin}:
        raise ExportRejected(f"constituent lists give {sorted(listed)} for {symbol!r}; the sidecar says {isin}")
    if isin not in index_universe_isins_as_of(ctx.session, as_of=doc.as_of):
        raise ExportRejected(f"{isin} is not in the universe as of {doc.as_of}")


def load_export(
    adapter: Adapter, ctx: AdapterContext, doc: RawDocument, isin: str, consolidation: Consolidation, tally: Tally
) -> None:
    """Parse one stored export into its rows, unless already done under RULE_VERSION."""
    tally.raw_files += 1
    loaded = screener_export_loaded_as_of(
        ctx.session, content_hash=doc.content_hash, file_as_of=doc.as_of, rule_version=RULE_VERSION, as_of=doc.as_of
    )
    if loaded is not None:
        tally.already_loaded += 1
        return
    try:
        check_identity(ctx, doc, isin, consolidation)
        parsed = parse_export(doc.data)
        exported_on = doc.as_of.astimezone(IST).date()
        if (latest := parsed.latest_period_end) is not None and latest >= exported_on:
            raise ExportRejected(f"a period ends {latest}, not before the export date {exported_on}")
    except ExportRejected as exc:
        tally.rejected_files += 1
        tally.problems.append(f"{doc.source_url} ({doc.content_hash[:12]}): {exc}")
        return

    base = dict(isin=isin, consolidation=consolidation, as_of=doc.as_of, content_hash=doc.content_hash,
                source_url=doc.source_url, extracted_by=adapter.extracted_by(), model_version=None,
                rule_version=RULE_VERSION)  # fmt: skip
    ctx.session.add(
        ScreenerExport(company_name=parsed.company_name, template_version=parsed.template_version,
                       rows_written=len(parsed.values), **base)  # fmt: skip
    )
    for v in parsed.values:
        ctx.session.add(
            ScreenerValue(statement=v.statement, line=v.line, period_end=v.period_end, value=v.value,
                          unit=v.unit, **base)  # fmt: skip
        )
    ctx.session.flush()
    tally.exports += 1
    tally.rows_written += len(parsed.values)


class ScreenerExportDrop(DropFolderAdapter):
    """Screener exports saved by hand into <EQUITY_DROP_FOLDER>/screener_export_drop/.

    Sidecar: `source_url` the company page
    (https://www.screener.in/company/TCS/consolidated/), `published_at` the
    export time (IST), `media_type` the xlsx type, `isin`, and `consolidation`
    ("consolidated" or "standalone", matching the URL).
    """

    name = "screener_export_drop"
    target_stores = TARGET_STORES
    meta_model = ScreenerExportMeta

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for path, meta in self.pending(ctx):
            assert isinstance(meta, ScreenerExportMeta)
            doc = self.store_raw(
                ctx, data=path.read_bytes(), source_url=meta.source_url, as_of=meta.published_at,
                media_type=meta.media_type,
            )  # fmt: skip
            load_export(self, ctx, doc, meta.isin, meta.consolidation, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, _ in self.pending(ctx):
            parse_export(path.read_bytes())

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        """Re-parse every stored file under the current RULE_VERSION, from blob storage, never re-fetching.

        The company comes from the file's earlier export rows, because the
        workbook does not name its ISIN. A file never loaded needs its
        sidecar: drop it again.
        """
        tally = Tally()
        now = ctx.now()
        known = {
            (e.content_hash, e.as_of): (e.isin, e.consolidation)
            for e in screener_exports_as_of(ctx.session, as_of=now)
        }
        for raw in raw_source_files_as_of(ctx.session, extracted_by=self.extracted_by(), as_of=now):
            company = known.get((raw.content_hash, raw.as_of))
            if company is None:
                tally.problems.append(f"{raw.source_url} ({raw.content_hash[:12]}): never loaded; drop it again")
                continue
            doc = RawDocument(
                data=ctx.blob.get(raw.content_hash), content_hash=raw.content_hash, source_url=raw.source_url,
                as_of=raw.as_of, fetched_at=raw.fetched_at, media_type=raw.media_type,
            )  # fmt: skip
            load_export(self, ctx, doc, *company, tally)
        return tally.result(self.name)
