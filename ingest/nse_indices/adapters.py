"""NSE Indices constituent-list adapters (CLAUDE.md "Universe").

Three sources of the same two files, sharing one parser and one loader:
- NseIndicesConstituents: today's lists from NSE's archive host (official_archive)
- NseIndicesConstituentsDrop: files downloaded by hand (manual_drop)
- WaybackIndexConstituents: dated past copies from the Internet Archive (web_scrape)

Each file becomes an `index_snapshot`. Membership intervals are computed from
snapshots at read time (core.db.pit.index_membership_as_of), so evidence that
arrives out of order never rewrites stored rows.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from pydantic import Field

from core.db.models import (
    Entity,
    EntityIsin,
    EntityLinkBasis,
    IndexSnapshot,
    IndexSnapshotConstituent,
    IndexSnapshotQuarantine,
    QuarantineReason,
)
from core.db.pit import (
    entity_isin_earliest_links_as_of,
    index_list_sources_settled_as_of,
    index_snapshot_keys_as_of,
    raw_source_files_as_of,
)
from core.sources import SourceClass
from ingest.base import (
    Adapter,
    AdapterContext,
    DropFolderAdapter,
    RawDocument,
    RunResult,
    RunStatus,
)
from ingest.http import AccessBlocked, PoliteClient
from ingest.nse_indices.parser import (
    LIST_FILES,
    RULE_VERSION,
    ListRejected,
    index_for_url,
    parse_constituent_list,
)
from ingest.schema import StrictModel

TARGET_STORES = (
    "index_snapshot",
    "index_snapshot_constituent",
    "index_snapshot_quarantine",
    "entity",
    "entity_isin",
)


# --------------------------------------------------------------------------- #
# Loader shared by every adapter
# --------------------------------------------------------------------------- #


@dataclass
class Tally:
    raw_files: int = 0
    snapshots: int = 0
    rows_written: int = 0
    quarantined: int = 0
    rejected_files: int = 0
    skipped: int = 0
    problems: list[str] = field(default_factory=list)

    def result(self, adapter: str) -> RunResult:
        """Failed if any file was rejected or the run stopped early: both need a person."""
        detail = (
            f"{self.snapshots} snapshots, {self.rejected_files} files rejected, "
            f"{self.skipped} already loaded"
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


def _provenance(adapter: Adapter, doc: RawDocument) -> dict:
    return dict(
        as_of=doc.as_of,
        content_hash=doc.content_hash,
        source_url=doc.source_url,
        extracted_by=adapter.extracted_by(),
        model_version=None,
    )


def wayback_digest(data: bytes) -> str:
    """The Wayback Machine's payload digest: base32 of SHA-1."""
    return base64.b32encode(hashlib.sha1(data, usedforsecurity=False).digest()).decode("ascii")


def load_list(
    adapter: Adapter,
    ctx: AdapterContext,
    doc: RawDocument,
    tally: Tally,
    *,
    expected_digest: str | None = None,
    archived_payload: bytes | None = None,
) -> None:
    """Parse one stored list into a snapshot, its constituents, quarantine rows and entities.

    `expected_digest` is checked against `archived_payload` (the bytes as the
    archive holds them, often gzip) when given, otherwise against the stored bytes.
    """
    tally.raw_files += 1
    prov = _provenance(adapter, doc)
    index_code = index_for_url(doc.source_url)
    try:
        if index_code is None:
            raise ListRejected(QuarantineReason.UNKNOWN_FILE, f"no index list at {doc.source_url}")
        payload = doc.data if archived_payload is None else archived_payload
        if expected_digest is not None and wayback_digest(payload) != expected_digest:
            raise ListRejected(
                QuarantineReason.DIGEST_MISMATCH, f"bytes do not match archive digest {expected_digest}"
            )
        known = index_snapshot_keys_as_of(
            ctx.session, source_url=doc.source_url, rule_version=RULE_VERSION, as_of=doc.as_of
        )
        if (index_code, doc.as_of) in known:
            tally.skipped += 1
            return
        parsed = parse_constituent_list(doc.data, index_code)
    except ListRejected as exc:
        ctx.session.add(
            IndexSnapshotQuarantine(
                index_code=index_code,
                reason=exc.reason,
                detail=exc.detail,
                rule_version=RULE_VERSION,
                **prov,
            )
        )
        ctx.session.flush()
        tally.rejected_files += 1
        tally.quarantined += 1
        return

    snapshot = IndexSnapshot(
        index_code=index_code,
        constituent_count=len(parsed.constituents),
        quarantined_rows=len(parsed.issues),
        rule_version=RULE_VERSION,
        **prov,
    )
    ctx.session.add(snapshot)
    ctx.session.flush()
    ctx.session.add_all(
        IndexSnapshotConstituent(
            snapshot_id=snapshot.id,
            isin=c.row.isin,
            symbol=c.row.symbol,
            series=c.row.series,
            company_name=c.row.company_name,
            industry=c.row.industry,
            row_number=c.row_number,
            **prov,
        )
        for c in parsed.constituents
    )
    ctx.session.add_all(
        IndexSnapshotQuarantine(
            snapshot_id=snapshot.id,
            index_code=index_code,
            row_number=issue.row_number,
            raw_row=issue.raw_row,
            reason=issue.reason,
            detail=issue.detail,
            rule_version=RULE_VERSION,
            **prov,
        )
        for issue in parsed.issues
    )
    _link_entities(adapter, ctx, doc, [c.row.isin for c in parsed.constituents])
    tally.snapshots += 1
    tally.rows_written += len(parsed.constituents)
    tally.quarantined += len(parsed.issues)


def _link_entities(adapter: Adapter, ctx: AdapterContext, doc: RawDocument, isins: list[str]) -> None:
    """Every listed ISIN has an entity; earlier evidence loaded later appends an earlier link."""
    prov = _provenance(adapter, doc)
    earliest = entity_isin_earliest_links_as_of(ctx.session, isins=isins, as_of=ctx.now())
    new = {isin: Entity(**prov) for isin in isins if isin not in earliest}
    ctx.session.add_all(new.values())
    ctx.session.flush()
    for isin, entity in new.items():
        ctx.session.add(
            EntityIsin(entity_id=entity.id, isin=isin, basis=EntityLinkBasis.FIRST_SEEN, **prov)
        )
    for isin, link in earliest.items():
        if doc.as_of < link.as_of:
            ctx.session.add(EntityIsin(entity_id=link.entity_id, isin=isin, basis=link.basis, **prov))
    ctx.session.flush()


def reparse_stored_lists(adapter: Adapter, ctx: AdapterContext) -> RunResult:
    """Re-parse `adapter`'s already-stored raw files under the current rule version.

    Reads bytes back from blob storage; never re-fetches over the network
    (CLAUDE.md Stack: "a parser fix must re-parse stored bytes without
    re-downloading"). A file already parsed under RULE_VERSION is skipped
    (load_list's own check via index_snapshot_keys_as_of); one that parses
    differently now adds a new snapshot alongside the old one (append-only),
    dated at the file's true, original as_of -- never today's -- so no
    history is invented (R2). Bytes are trusted as already verified (stored
    by a prior successful `ingest`), so no digest re-check is made here.
    """
    tally = Tally()
    for raw in raw_source_files_as_of(ctx.session, extracted_by=adapter.extracted_by(), as_of=ctx.now()):
        if index_for_url(raw.source_url) is None:
            continue  # not a constituent list (e.g. a stored Wayback CDX query response)
        doc = RawDocument(
            data=ctx.blob.get(raw.content_hash),
            content_hash=raw.content_hash,
            source_url=raw.source_url,
            as_of=raw.as_of,
            fetched_at=raw.fetched_at,
            media_type=raw.media_type,
        )
        load_list(adapter, ctx, doc, tally)
    return tally.result(adapter.name)


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()


class _RunStopped(Exception):
    """Stop the run and keep what was loaded; never retry (CLAUDE.md "Breakage")."""


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class NseIndicesConstituents(Adapter):
    """Today's lists from NSE's archive host.

    The file has no publication date and a server's Last-Modified header is not
    a publication time, so `as_of` is the fetch time: late, never early (R2).
    """

    name = "nse_indices_constituents"
    source_class = SourceClass.OFFICIAL_ARCHIVE
    target_stores = TARGET_STORES

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return PoliteClient(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.nse_indices_min_interval_seconds,
            respect_robots=True,
            transport=self._transport,
        )

    def _urls(self, ctx: AdapterContext) -> list[str]:
        base = ctx.settings.nse_indices_archive_base_url.rstrip("/")
        return [f"{base}/{filename}" for filename in LIST_FILES]

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        with self._client(ctx) as http:
            for url in self._urls(ctx):
                try:
                    response = http.get(url)
                except (AccessBlocked, httpx.HTTPError) as exc:
                    tally.problems.append(f"stopped at {url}: {exc}")
                    break
                doc = self.store_raw(
                    ctx, data=response.content, source_url=url, as_of=ctx.now(),
                    media_type=_media_type(response),
                )  # fmt: skip
                load_list(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        with self._client(ctx) as http:
            for url in self._urls(ctx):
                parse_constituent_list(http.get(url).content, index_for_url(url))

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_lists(self, ctx)


class NseIndicesConstituentsDrop(DropFolderAdapter):
    """Lists downloaded by hand into <EQUITY_DROP_FOLDER>/nse_indices_constituents_drop/.

    For niftyindices.com, which refuses automated clients, or for a Wayback
    capture (sidecar `source_url` = the capture URL, `published_at` = the capture
    time). The index comes from the file name in `source_url`.
    """

    name = "nse_indices_constituents_drop"
    target_stores = TARGET_STORES

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        for doc in self.drop_files(ctx):
            load_list(self, ctx, doc, tally)
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        for path, meta in self.pending(ctx):
            if (index_code := index_for_url(meta.source_url)) is None:
                raise ListRejected(QuarantineReason.UNKNOWN_FILE, meta.source_url)
            parse_constituent_list(path.read_bytes(), index_code)

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_lists(self, ctx)


CDX_FIELDS = ("timestamp", "original", "mimetype", "statuscode", "digest")


class CdxShapeError(ValueError):
    pass


class CdxCapture(StrictModel):
    timestamp: str = Field(pattern=r"^\d{14}$")
    original: str = Field(min_length=1)
    mimetype: str
    statuscode: str = Field(pattern=r"^200$")
    digest: str = Field(pattern=r"^[A-Z2-7]{32}$")

    @property
    def captured_at(self) -> datetime:
        return datetime.strptime(self.timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def parse_cdx(data: bytes) -> list[CdxCapture]:
    """A CDX `output=json` response: a header row naming CDX_FIELDS, then one row per capture."""
    rows = json.loads(data)
    if rows == []:
        return []
    if not (isinstance(rows, list) and isinstance(rows[0], list) and tuple(rows[0]) == CDX_FIELDS):
        raise CdxShapeError(f"CDX header is not {list(CDX_FIELDS)}: {str(rows)[:200]}")
    try:
        return [CdxCapture(**dict(zip(CDX_FIELDS, row, strict=True))) for row in rows[1:]]
    except (TypeError, ValueError) as exc:
        raise CdxShapeError(f"CDX row does not match {list(CDX_FIELDS)}: {exc}") from None


def run_boundaries(captures: list[CdxCapture]) -> list[CdxCapture]:
    """First and last capture of each run of identical content, in time order.

    Identical bytes in between add nothing: membership between two snapshots
    that agree is already taken as unchanged.
    """
    ordered = sorted(captures, key=lambda c: (c.timestamp, c.original))
    return [
        c
        for i, c in enumerate(ordered)
        if not (
            0 < i < len(ordered) - 1
            and ordered[i - 1].digest == c.digest == ordered[i + 1].digest
        )
    ]


class WaybackIndexConstituents(Adapter):
    """Past copies of the lists from the Internet Archive's Wayback Machine.

    `as_of` is the capture time: the list was public at the latest then (R2).
    Captures are sparse, months or years apart, and the gaps stay uncertain
    (core.compute.membership). Each capture's bytes as transferred (often
    gzip) are checked against the archive's digest; the decoded list is stored.
    Overload (503/504, timeouts) is retried a bounded number of times; a block,
    a redirect to another capture or retries running out stops the run and
    keeps what was loaded.

    A capture is skipped once it is a snapshot, or was quarantined whole under
    the current rule version; a rule change examines older quarantine again.
    """

    name = "wayback_nse_indices_constituents"
    source_class = SourceClass.WEB_SCRAPE
    target_stores = TARGET_STORES

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self, ctx: AdapterContext) -> PoliteClient:
        return PoliteClient(
            user_agent=ctx.settings.http_user_agent,
            min_interval_seconds=ctx.settings.wayback_min_interval_seconds,
            respect_robots=True,
            timeout_seconds=ctx.settings.wayback_timeout_seconds,
            overload_retries=ctx.settings.wayback_overload_retries,
            retry_backoff_seconds=ctx.settings.wayback_retry_backoff_seconds,
            transport=self._transport,
        )

    def _cdx(self, http: PoliteClient, ctx: AdapterContext, list_url: str, **params: str) -> httpx.Response:
        return http.get(
            ctx.settings.wayback_cdx_url,
            params={"url": list_url, "output": "json", "fl": ",".join(CDX_FIELDS),
                    "filter": "statuscode:200", **params},
        )  # fmt: skip

    def _capture_url(self, ctx: AdapterContext, capture: CdxCapture) -> str:
        return ctx.settings.wayback_capture_url.format(
            timestamp=capture.timestamp, original=capture.original
        )

    def _fetch_capture(
        self, http: PoliteClient, ctx: AdapterContext, capture: CdxCapture
    ) -> tuple[str, httpx.Response, bytes]:
        url = self._capture_url(ctx, capture)
        response, raw = http.get_with_raw(url)
        if response.url != httpx.URL(url):
            raise _RunStopped(f"{url} redirected to {response.url}")
        return url, response, raw

    def ingest(self, ctx: AdapterContext) -> RunResult:
        tally = Tally()
        settled = index_list_sources_settled_as_of(
            ctx.session, extracted_by=self.extracted_by(), rule_version=RULE_VERSION, as_of=ctx.now()
        )
        with self._client(ctx) as http:
            try:
                for filename in LIST_FILES:
                    captures = []
                    for location in ctx.settings.wayback_list_locations:
                        response = self._cdx(http, ctx, f"{location.rstrip('/')}/{filename}")
                        doc = self.store_raw(
                            ctx, data=response.content, source_url=str(response.url),
                            as_of=ctx.now(), media_type=_media_type(response),
                        )  # fmt: skip
                        captures += parse_cdx(doc.data)
                    for capture in run_boundaries(captures):
                        if self._capture_url(ctx, capture) in settled:
                            tally.skipped += 1
                            continue
                        url, response, raw = self._fetch_capture(http, ctx, capture)
                        doc = self.store_raw(
                            ctx, data=response.content, source_url=url,
                            as_of=capture.captured_at, media_type=_media_type(response),
                        )  # fmt: skip
                        settled.add(url)
                        load_list(
                            self, ctx, doc, tally, expected_digest=capture.digest, archived_payload=raw
                        )
            except (AccessBlocked, httpx.HTTPError, CdxShapeError, _RunStopped) as exc:
                tally.problems.append(f"stopped: {exc}")
        return tally.result(self.name)

    def check_shape(self, ctx: AdapterContext) -> None:
        location = ctx.settings.wayback_list_locations[0].rstrip("/")
        filename = next(iter(LIST_FILES))
        with self._client(ctx) as http:
            captures = parse_cdx(self._cdx(http, ctx, f"{location}/{filename}", limit="1").content)
            if not captures:
                raise CdxShapeError(f"no captures listed for {location}/{filename}")
            url, response, raw = self._fetch_capture(http, ctx, captures[0])
            if wayback_digest(raw) != captures[0].digest:
                raise ListRejected(QuarantineReason.DIGEST_MISMATCH, url)
            parse_constituent_list(response.content, LIST_FILES[filename])

    def _reparse(self, ctx: AdapterContext) -> RunResult:
        return reparse_stored_lists(self, ctx)
