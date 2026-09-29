"""Point-in-time reads. Every read takes an explicit `as_of`; there is no default."""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.compute import ratios
from core.compute.adjustment import (
    PriceFactor,
    adjust_series,
    bonus_factor,
    demerger_factor,
    rights_factor,
    split_factor,
)
from core.compute.drift import DriftWatch, MissingInputReason, drift_watch
from core.compute.membership import (
    Membership,
    ObservedInterval,
    Snapshot,
    membership_on,
    observed_intervals,
)
from core.compute.guidance import (
    ClaimSection,
    ComparableClaim,
    Direction,
    HedgeChange,
    HedgeStrength,
    ParsedValue,
    Specificity,
    Unit,
    compare_claims,
    compare_hedges,
)
from core.compute.guidance_report import (
    ClaimView,
    GuidanceReport,
    InputFact,
    SourceRef,
    build_report,
)
from core.compute.guidance_resolution import (
    METRIC_RESOLVERS,
    Basis,
    ClaimOutcome,
    DeliveryRate,
    Fact,
    PeriodKey,
    Resolution,
    Status,
    TranscriptMentions,
    UnresolvableReason,
    claim_key,
    delivery_rate,
    detect_silent,
    period_resolved,
    resolve_claim,
    select_claims,
)
from core.compute.price_crosscheck import Bar, Mismatch, crosscheck
from core.db.models import (
    BulkBlockDeal,
    IndexEvent,
    IndexEventStatus,
    InsiderTrade,
    ScheduledEvent,
    StakeDisclosure,
    ConcallDocument,
    ConcallTranscript,
    CompanyEvent,
    CompanyEventType,
    Consolidation,
    CorporateAction,
    CorporateActionQuarantine,
    CorporateActionStatus,
    CorporateActionType,
    EntityIsin,
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFiling,
    GuidanceClaim,
    GuidanceQuarantine,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    IndexSnapshotQuarantine,
    NseBhavcopyQuarantine,
    NseBhavcopyRow,
    RatingAction,
    RatingAgency,
    RatioBasis,
    RawSourceFile,
    ScreenerExport,
    ScreenerValue,
    ShareholdingFiling,
    ShareholdingPattern,
    ShareholdingQuarantine,
    UpstoxCandle,
    UpstoxCandleQuarantine,
    TechnicalSignal,
    WatchlistEntry,
)
from core.timezones import IST, require_aware


def facts_as_of(
    session: Session,
    *,
    isin: str,
    consolidation: Consolidation,
    as_of: datetime,
    line_items: Sequence[str] | None = None,
) -> list[FinancialFact]:
    """What the system knew about `isin` at `as_of`.

    For each (line_item, period) returns the latest version with
    `as_of <= as_of`, so later restatements are invisible to earlier reads.
    """
    require_aware(as_of, "as_of")
    f = FinancialFact
    stmt = (
        select(f)
        .where(f.isin == isin, f.consolidation == consolidation, f.as_of <= as_of)
        .distinct(f.line_item, f.period_start, f.period_end)
        .order_by(f.line_item, f.period_start, f.period_end, f.as_of.desc(), f.id.desc())
    )
    if line_items is not None:
        stmt = stmt.where(f.line_item.in_(line_items))
    return list(session.scalars(stmt))


def raw_source_file_as_of(
    session: Session, *, content_hash: str, file_as_of: datetime, as_of: datetime
) -> RawSourceFile | None:
    """One stored file's record, for the media type and URL a later step needs to re-read it."""
    require_aware(as_of, "as_of")
    r = RawSourceFile
    return session.scalars(
        select(r)
        .where(r.content_hash == content_hash, r.as_of == file_as_of, r.as_of <= as_of)
        .order_by(r.id)
        .limit(1)
    ).first()


def raw_source_files_as_of(
    session: Session, *, extracted_by: str, as_of: datetime
) -> list[RawSourceFile]:
    """Raw files an adapter stored whose content was public at `as_of`, oldest first.

    A parser fix re-parses these from blob storage instead of re-downloading.
    """
    require_aware(as_of, "as_of")
    r = RawSourceFile
    stmt = (
        select(r)
        .where(r.extracted_by == extracted_by, r.as_of <= as_of)
        .order_by(r.as_of, r.id)
    )
    return list(session.scalars(stmt))


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


def entity_isin_earliest_links_as_of(
    session: Session, *, isins: Collection[str], as_of: datetime
) -> dict[str, EntityIsin]:
    """For each ISIN linked to an entity by `as_of`, its earliest known link."""
    require_aware(as_of, "as_of")
    if not isins:
        return {}
    e = EntityIsin
    stmt = (
        select(e)
        .where(e.isin.in_(list(isins)), e.as_of <= as_of)
        .distinct(e.isin)
        .order_by(e.isin, e.as_of, e.id)
    )
    return {link.isin: link for link in session.scalars(stmt)}


# --------------------------------------------------------------------------- #
# Index membership
# --------------------------------------------------------------------------- #


def index_snapshots_as_of(
    session: Session, *, index_code: IndexCode, as_of: datetime
) -> list[Snapshot]:
    """Constituent lists for `index_code` published by `as_of`, oldest first.

    A file reparsed under a corrected rule_version adds a row rather than
    editing the old one (append-only); this keeps only the newest row per
    (source_url, as_of) -- ids are assigned in insertion order, so the
    highest id is always the most recently produced parse, whatever the
    rule_version string happens to say.
    """
    require_aware(as_of, "as_of")
    s, c = IndexSnapshot, IndexSnapshotConstituent
    all_snapshots = list(
        session.scalars(select(s).where(s.index_code == index_code, s.as_of <= as_of).order_by(s.id))
    )
    latest_by_file: dict[tuple[str, datetime], IndexSnapshot] = {}
    for snap in all_snapshots:
        latest_by_file[(snap.source_url, snap.as_of)] = snap  # ascending id: last write wins
    snapshots = sorted(latest_by_file.values(), key=lambda snap: (snap.as_of, snap.id))

    ids = [snap.id for snap in snapshots]
    isins: dict[int, set[str]] = {snap.id: set() for snap in snapshots}
    if ids:
        rows = session.execute(
            select(c.snapshot_id, c.isin).where(c.snapshot_id.in_(ids), c.as_of <= as_of)
        )
        for snapshot_id, isin in rows:
            isins[snapshot_id].add(isin)
    return [
        Snapshot(snap.as_of, frozenset(isins[snap.id]), complete=snap.quarantined_rows == 0)
        for snap in snapshots
    ]


def index_membership_as_of(
    session: Session, *, index_code: IndexCode, as_of: datetime
) -> list[ObservedInterval]:
    """Membership intervals as they could be derived from lists published by `as_of`."""
    return observed_intervals(index_snapshots_as_of(session, index_code=index_code, as_of=as_of))


def index_members_on(
    session: Session, *, index_code: IndexCode, on: datetime, as_of: datetime
) -> Membership:
    """Members of `index_code` at `on`, using only lists published by `as_of`.

    `on` selects the date; `as_of` decides what is visible. Never pass today's
    `as_of` for a replay at an earlier `t` (R2).
    """
    require_aware(on, "on")
    return membership_on(index_membership_as_of(session, index_code=index_code, as_of=as_of), on)


@dataclass(frozen=True)
class ListedSnapshot:
    as_of: datetime
    content_hash: str
    isins: frozenset[str]
    complete: bool


def index_lists_as_of(session: Session, *, index_code: IndexCode, as_of: datetime) -> list[ListedSnapshot]:
    """Each constituent list of `index_code` published by `as_of`, with its file's hash, oldest first.

    Like index_snapshots_as_of, only the newest parse of each file counts.
    """
    require_aware(as_of, "as_of")
    s, c = IndexSnapshot, IndexSnapshotConstituent
    latest: dict[tuple[str, datetime], IndexSnapshot] = {}
    for snap in session.scalars(select(s).where(s.index_code == index_code, s.as_of <= as_of).order_by(s.id)):
        latest[(snap.source_url, snap.as_of)] = snap
    snapshots = sorted(latest.values(), key=lambda snap: (snap.as_of, snap.id))
    isins: dict[int, set[str]] = {snap.id: set() for snap in snapshots}
    if isins:
        for snapshot_id, isin in session.execute(
            select(c.snapshot_id, c.isin).where(c.snapshot_id.in_(isins), c.as_of <= as_of)
        ):
            isins[snapshot_id].add(isin)
    return [
        ListedSnapshot(snap.as_of, snap.content_hash, frozenset(isins[snap.id]), snap.quarantined_rows == 0)
        for snap in snapshots
    ]


def index_list_names_as_of(session: Session, *, content_hash: str, as_of: datetime) -> dict[str, str]:
    """ISIN -> company name in the constituent list file `content_hash`, as parsed by `as_of`.

    The newest parse of the file wins, as in index_lists_as_of.
    """
    require_aware(as_of, "as_of")
    c = IndexSnapshotConstituent
    rows = session.execute(
        select(c.isin, c.company_name).where(c.content_hash == content_hash, c.as_of <= as_of).order_by(c.id)
    )
    return {isin: name for isin, name in rows}


def index_snapshot_keys_as_of(
    session: Session, *, source_url: str, rule_version: str, as_of: datetime
) -> set[tuple[IndexCode, datetime]]:
    """(index, publication time) of snapshots from `source_url` already parsed under `rule_version`.

    A file with a snapshot only from an older rule is not considered known
    here, so a loader reparses it (`reparse_stored_lists`) rather than
    skipping it.
    """
    require_aware(as_of, "as_of")
    s = IndexSnapshot
    rows = session.execute(
        select(s.index_code, s.as_of).where(
            s.source_url == source_url, s.rule_version == rule_version, s.as_of <= as_of
        )
    )
    return {(index_code, published) for index_code, published in rows}


def index_list_sources_settled_as_of(
    session: Session, *, extracted_by: str, rule_version: str, as_of: datetime
) -> set[str]:
    """Source URLs `extracted_by` turned into a snapshot, or quarantined whole under `rule_version`.

    Adapters skip these. A whole-file quarantine under an older rule is
    examined again after the rule changes.
    """
    require_aware(as_of, "as_of")
    s, q = IndexSnapshot, IndexSnapshotQuarantine
    snapshots = session.scalars(
        select(s.source_url).where(s.extracted_by == extracted_by, s.as_of <= as_of)
    )
    quarantined = session.scalars(
        select(q.source_url).where(
            q.extracted_by == extracted_by,
            q.rule_version == rule_version,
            q.snapshot_id.is_(None),
            q.as_of <= as_of,
        )
    )
    return set(snapshots) | set(quarantined)


def index_snapshot_quarantine_as_of(
    session: Session, *, as_of: datetime
) -> list[IndexSnapshotQuarantine]:
    """Lists and rows held back for review, for files published by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = IndexSnapshotQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class QuarantineEntry:
    row: IndexSnapshotQuarantine
    # A snapshot later loaded from the same file (same source URL and publication time).
    superseded_by: int | None

    @property
    def needs_review(self) -> bool:
        return self.superseded_by is None


def index_quarantine_review_as_of(session: Session, *, as_of: datetime) -> list[QuarantineEntry]:
    """Every quarantine row for files published by `as_of`, marked when it no longer needs review.

    A whole-file entry is superseded once the same file has been loaded as a
    snapshot (e.g. after a rule fix reparses stored bytes -- see
    `reparse_stored_lists` in ingest/nse_indices/adapters.py). A row-level
    entry is superseded once its *own* snapshot has itself been superseded by
    a later one for the same file: the issue that held the row back may no
    longer apply under the newer rule. Quarantine rows are never deleted
    (append-only); only their review status is computed at read time.
    """
    rows = index_snapshot_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []

    s = IndexSnapshot
    row_level_snapshot_ids = {q.snapshot_id for q in rows if q.snapshot_id is not None}
    own_key: dict[int, tuple[str, datetime]] = {}
    if row_level_snapshot_ids:
        stmt = select(s.id, s.source_url, s.as_of).where(s.id.in_(row_level_snapshot_ids))
        own_key = {snap_id: (url, published) for snap_id, url, published in session.execute(stmt)}

    keys = {(q.source_url, q.as_of) for q in rows if q.snapshot_id is None} | set(own_key.values())
    newest: dict[tuple[str, datetime], int] = {}
    if keys:
        urls = {url for url, _ in keys}
        stmt = (
            select(s.source_url, s.as_of, s.id)
            .where(s.source_url.in_(urls), s.as_of <= as_of)
            .order_by(s.id)
        )
        for url, published, snap_id in session.execute(stmt):
            if (url, published) in keys:
                newest[(url, published)] = snap_id  # ascending id: last write wins

    entries = []
    for q in rows:
        if q.snapshot_id is None:
            entries.append(QuarantineEntry(q, newest.get((q.source_url, q.as_of))))
        else:
            key = own_key.get(q.snapshot_id)
            latest_id = newest.get(key) if key else None
            superseded = latest_id if latest_id is not None and latest_id != q.snapshot_id else None
            entries.append(QuarantineEntry(q, superseded))
    return entries


# --------------------------------------------------------------------------- #
# NSE bhavcopy: price cross-check and the dated ticker -> ISIN map
# --------------------------------------------------------------------------- #


def bhavcopy_sources_settled_as_of(
    session: Session, *, extracted_by: str, rule_version: str, as_of: datetime
) -> set[str]:
    """Source URLs (one per trade date) `extracted_by` already turned into rows,
    or quarantined whole, under `rule_version`. An adapter skips these.
    """
    require_aware(as_of, "as_of")
    r, q = NseBhavcopyRow, NseBhavcopyQuarantine
    rows = session.scalars(
        select(r.source_url).where(
            r.extracted_by == extracted_by, r.rule_version == rule_version, r.as_of <= as_of
        )
    )
    whole_file = session.scalars(
        select(q.source_url).where(
            q.extracted_by == extracted_by,
            q.rule_version == rule_version,
            q.row_number.is_(None),
            q.as_of <= as_of,
        )
    )
    return set(rows) | set(whole_file)


def bhavcopy_source_loaded_as_of(
    session: Session, *, source_url: str, extracted_by: str, rule_version: str, as_of: datetime
) -> bool:
    """Whether `source_url` already has a row or a whole-file quarantine under `rule_version`.

    The correctness backstop `load_bhavcopy` itself checks, so a drop-folder
    rerun or a reparse of an already-current file is a no-op rather than a
    duplicate insert; `bhavcopy_sources_settled_as_of` is the batch version
    the archive adapter uses to skip fetching bytes it already has.
    """
    require_aware(as_of, "as_of")
    r, q = NseBhavcopyRow, NseBhavcopyQuarantine
    has_row = session.scalar(
        select(r.id)
        .where(r.source_url == source_url, r.extracted_by == extracted_by,
               r.rule_version == rule_version, r.as_of <= as_of)
        .limit(1)
    )  # fmt: skip
    if has_row is not None:
        return True
    has_quarantine = session.scalar(
        select(q.id)
        .where(q.source_url == source_url, q.extracted_by == extracted_by,
               q.rule_version == rule_version, q.row_number.is_(None), q.as_of <= as_of)
        .limit(1)
    )  # fmt: skip
    return has_quarantine is not None


def bhavcopy_latest_trade_date_as_of(session: Session, *, extracted_by: str, as_of: datetime) -> date | None:
    """The latest trade_date `extracted_by` has stored a row for, known by `as_of`.

    The archive adapter's resume cursor: walking every day from scratch on
    every run would re-check every past holiday's URL forever (a 404, not a
    stored fact -- nothing marks a holiday "settled"). Starting the day after
    this bounds a rerun's wasted checks to the current holiday streak, not
    the whole history. `bhavcopy_sources_settled_as_of` still guards
    correctness for any gap this cursor does not cover (e.g. a whole-file
    quarantine with no row of its own).
    """
    require_aware(as_of, "as_of")
    r = NseBhavcopyRow
    return session.scalar(
        select(r.trade_date)
        .where(r.extracted_by == extracted_by, r.as_of <= as_of)
        .order_by(r.trade_date.desc())
        .limit(1)
    )


def bhavcopy_rows_as_of(
    session: Session, *, isin: str, start: date, end: date, as_of: datetime
) -> list[NseBhavcopyRow]:
    """Bhavcopy rows for `isin` with trade_date in [start, end], published by `as_of`, oldest first.

    A reparse under a corrected rule_version adds a row rather than editing
    the old one (append-only); this keeps only the newest row per trade_date.
    """
    require_aware(as_of, "as_of")
    r = NseBhavcopyRow
    all_rows = list(
        session.scalars(
            select(r)
            .where(r.isin == isin, r.trade_date >= start, r.trade_date <= end, r.as_of <= as_of)
            .order_by(r.id)
        )
    )
    latest_by_date: dict[date, NseBhavcopyRow] = {}
    for row in all_rows:
        latest_by_date[row.trade_date] = row  # ascending id: last write wins
    return sorted(latest_by_date.values(), key=lambda row: row.trade_date)


def symbol_to_isin_as_of(
    session: Session, *, symbol: str, on: date, as_of: datetime, since: date | None = None
) -> str | None:
    """The ISIN bhavcopy showed for `symbol` on the latest trade_date <= `on`, known by `as_of`.

    Symbols are reused across companies over decades (CLAUDE.md "Universe"),
    so this is a snapshot at a date, never a standing lookup table. None if
    `symbol` never traded on or before `on` in what is known by `as_of`, or
    not since `since` when that is given (a stale row may be another company).
    """
    require_aware(as_of, "as_of")
    r = NseBhavcopyRow
    stmt = select(r).where(r.symbol == symbol, r.trade_date <= on, r.as_of <= as_of).order_by(r.id)
    if since is not None:
        stmt = stmt.where(r.trade_date >= since)
    rows = list(session.scalars(stmt))
    if not rows:
        return None
    latest_by_date: dict[date, NseBhavcopyRow] = {}
    for row in rows:
        latest_by_date[row.trade_date] = row  # ascending id: last write wins (reparse safety)
    return latest_by_date[max(latest_by_date)].isin


def bhavcopy_quarantine_as_of(session: Session, *, as_of: datetime) -> list[NseBhavcopyQuarantine]:
    """Bhavcopy files and rows held back for review, published by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = NseBhavcopyQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class BhavcopyQuarantineEntry:
    row: NseBhavcopyQuarantine
    resolved: bool  # a matching row now exists, e.g. after a rule fix reparsed the file

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def bhavcopy_quarantine_review_as_of(session: Session, *, as_of: datetime) -> list[BhavcopyQuarantineEntry]:
    """Every bhavcopy quarantine row for files published by `as_of`, marked once resolved.

    A row-level entry is resolved once a valid row now exists for the same
    file (source_url) and (isin, series); a whole-file entry is resolved once
    ANY row now exists for that file. Quarantine rows are never deleted
    (append-only); only their review status is computed at read time.
    """
    rows = bhavcopy_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    r = NseBhavcopyRow
    urls = {row.source_url for row in rows}
    existing = session.execute(
        select(r.source_url, r.isin, r.series).where(r.source_url.in_(urls), r.as_of <= as_of)
    )
    by_url_key: dict[str, set[tuple[str, str]]] = {}
    any_row_for_url: set[str] = set()
    for url, isin, series in existing:
        by_url_key.setdefault(url, set()).add((isin, series))
        any_row_for_url.add(url)

    entries = []
    for row in rows:
        if row.row_number is None:
            resolved = row.source_url in any_row_for_url
        else:
            resolved = (row.isin, row.series) in by_url_key.get(row.source_url, set())
        entries.append(BhavcopyQuarantineEntry(row, resolved))
    return entries


def index_universe_isins_as_of(session: Session, *, as_of: datetime) -> set[str]:
    """Every ISIN listed in any Nifty 50 / Next 50 constituent list published by `as_of`.

    The price universe: a company that was ever a member needs its history,
    so this is the union over all lists, never today's list alone
    (survivorship bias, CLAUDE.md "Universe").
    """
    require_aware(as_of, "as_of")
    s, c = IndexSnapshot, IndexSnapshotConstituent
    rows = session.scalars(
        select(c.isin).join(s, s.id == c.snapshot_id).where(s.as_of <= as_of, c.as_of <= as_of).distinct()
    )
    return set(rows)


def index_symbol_isins_as_of(
    session: Session, *, symbol: str, since: datetime, as_of: datetime
) -> set[str]:
    """ISINs that constituent lists published between `since` and `as_of` gave for `symbol`.

    A second, independent symbol -> ISIN read for XBRL filings that carry no
    ISIN. More than one ISIN means the symbol changed hands in the window.
    """
    require_aware(as_of, "as_of")
    require_aware(since, "since")
    s, c = IndexSnapshot, IndexSnapshotConstituent
    rows = session.scalars(
        select(c.isin)
        .join(s, s.id == c.snapshot_id)
        .where(c.symbol == symbol, s.as_of >= since, s.as_of <= as_of, c.as_of <= as_of)
        .distinct()
    )
    return set(rows)


# --------------------------------------------------------------------------- #
# Upstox daily candles: the vendor's price history
# --------------------------------------------------------------------------- #


def upstox_source_loaded_as_of(
    session: Session, *, source_url: str, fetched_as_of: datetime, extracted_by: str, rule_version: str
) -> bool:
    """Whether the response fetched from `source_url` at `fetched_as_of` already has a
    candle or a whole-response quarantine under `rule_version`.

    The loader's backstop, so a drop-folder rerun or a reparse of a current
    response is a no-op rather than a duplicate insert. `fetched_as_of` is
    both the response's identity and the visibility bound.
    """
    require_aware(fetched_as_of, "fetched_as_of")
    c, q = UpstoxCandle, UpstoxCandleQuarantine
    has_candle = session.scalar(
        select(c.id)
        .where(c.source_url == source_url, c.as_of == fetched_as_of, c.extracted_by == extracted_by,
               c.rule_version == rule_version)
        .limit(1)
    )  # fmt: skip
    if has_candle is not None:
        return True
    has_quarantine = session.scalar(
        select(q.id)
        .where(q.source_url == source_url, q.as_of == fetched_as_of, q.extracted_by == extracted_by,
               q.rule_version == rule_version, q.candle_index.is_(None))
        .limit(1)
    )  # fmt: skip
    return has_quarantine is not None


def upstox_latest_trade_date_as_of(
    session: Session, *, isin: str, extracted_by: str, as_of: datetime
) -> date | None:
    """The latest trade_date `extracted_by` stored a candle for `isin`, known by `as_of`.

    The adapter's per-ISIN resume cursor: the next request starts the day after.
    """
    require_aware(as_of, "as_of")
    c = UpstoxCandle
    return session.scalar(
        select(c.trade_date)
        .where(c.isin == isin, c.extracted_by == extracted_by, c.as_of <= as_of)
        .order_by(c.trade_date.desc())
        .limit(1)
    )


def upstox_candles_as_of(
    session: Session, *, isin: str, start: date, end: date, as_of: datetime
) -> list[UpstoxCandle]:
    """Upstox candles for `isin` with trade_date in [start, end], fetched by `as_of`, oldest first.

    A later fetch of the same day (the vendor's newer view) or a reparse
    under a corrected rule_version adds a row rather than editing the old one;
    this keeps the newest per trade_date: latest `as_of`, then highest id.
    """
    require_aware(as_of, "as_of")
    c = UpstoxCandle
    rows = session.scalars(
        select(c)
        .where(c.isin == isin, c.trade_date >= start, c.trade_date <= end, c.as_of <= as_of)
        .order_by(c.as_of, c.id)
    )
    latest_by_date: dict[date, UpstoxCandle] = {}
    for row in rows:
        latest_by_date[row.trade_date] = row  # ascending (as_of, id): last write wins
    return sorted(latest_by_date.values(), key=lambda row: row.trade_date)


def upstox_bhavcopy_crosscheck_as_of(
    session: Session, *, isin: str, start: date, end: date, as_of: datetime
) -> list[Mismatch]:
    """Upstox candles vs. NSE bhavcopy rows for `isin`, over the dates both cover, as known by `as_of`.

    The window is trimmed to the overlap of what each source has for `isin`
    (bhavcopy only starts 2024-07-08), so a date outside one source's
    coverage is not reported as missing. Empty when they do not overlap.
    """
    vendor = {
        r.trade_date: Bar(r.open, r.high, r.low, r.close, r.volume)
        for r in upstox_candles_as_of(session, isin=isin, start=start, end=end, as_of=as_of)
    }
    exchange = {
        r.trade_date: Bar(r.open, r.high, r.low, r.close, r.volume)
        for r in bhavcopy_rows_as_of(session, isin=isin, start=start, end=end, as_of=as_of)
    }
    if not vendor or not exchange:
        return []
    lo, hi = max(min(vendor), min(exchange)), min(max(vendor), max(exchange))
    return crosscheck(
        {d: b for d, b in vendor.items() if lo <= d <= hi},
        {d: b for d, b in exchange.items() if lo <= d <= hi},
    )


def upstox_quarantine_as_of(session: Session, *, as_of: datetime) -> list[UpstoxCandleQuarantine]:
    """Upstox responses and candles held back for review, fetched by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = UpstoxCandleQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class UpstoxQuarantineEntry:
    row: UpstoxCandleQuarantine
    resolved: bool  # the response was re-parsed under the current rule and this was not quarantined again

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def upstox_quarantine_review_as_of(
    session: Session, *, current_rule_version: str, as_of: datetime
) -> list[UpstoxQuarantineEntry]:
    """Every Upstox quarantine row known by `as_of`, marked once a rule fix has dealt with it.

    A response is identified by (source_url, as_of). An entry under an older
    rule is resolved once that response has been parsed under
    `current_rule_version` (it has a candle or a quarantine row under it) and
    that parse did not quarantine the same candle (same `candle_index`, or
    the whole response) again. A malformed candle may carry no trade_date, so
    matching is by position in the response, not by date. Entries under the
    current rule always need review. Quarantine rows are never deleted
    (append-only).
    """
    rows = upstox_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    c = UpstoxCandle
    urls = {row.source_url for row in rows}
    parsed_now = set(
        session.execute(
            select(c.source_url, c.as_of)
            .where(c.source_url.in_(urls), c.rule_version == current_rule_version, c.as_of <= as_of)
            .distinct()
        ).tuples()
    )
    requarantined = set()
    for row in rows:
        if row.rule_version == current_rule_version:
            parsed_now.add((row.source_url, row.as_of))
            requarantined.add((row.source_url, row.as_of, row.candle_index))

    entries = []
    for row in rows:
        response = (row.source_url, row.as_of)
        resolved = (
            row.rule_version != current_rule_version
            and response in parsed_now
            and (*response, row.candle_index) not in requarantined
        )
        entries.append(UpstoxQuarantineEntry(row, resolved))
    return entries


# --------------------------------------------------------------------------- #
# Corporate actions: versions, ISIN lineage, price adjustment factors
# --------------------------------------------------------------------------- #

# Actions that change the price series (core.compute.adjustment); dividends do not.
CAPITAL_ACTION_TYPES = (
    CorporateActionType.SPLIT,
    CorporateActionType.CONSOLIDATION,
    CorporateActionType.BONUS,
    CorporateActionType.RIGHTS,
    CorporateActionType.DEMERGER,
)
_PRELIMINARY = frozenset({CorporateActionStatus.ANNOUNCED, CorporateActionStatus.APPROVED})
_ADJUSTING_BASES = frozenset({RatioBasis.EXCHANGE_FIELD, RatioBasis.HUMAN_VERIFIED})
# How far back to look for the last cum-rights close before a rights ex-date.
CUM_CLOSE_LOOKBACK = timedelta(days=30)


def _newest_per_key(rows: Iterable[CorporateAction]) -> list[CorporateAction]:
    latest: dict[str, CorporateAction] = {}
    for row in sorted(rows, key=lambda r: (r.as_of, r.id)):
        latest[row.action_key] = row  # ascending (as_of, id): last write wins
    return sorted(latest.values(), key=lambda r: (r.ex_date or date.max, r.action_key))


def corporate_actions_as_of(
    session: Session,
    *,
    isins: Collection[str],
    as_of: datetime,
    action_types: Collection[CorporateActionType] | None = None,
) -> list[CorporateAction]:
    """The newest version, known by `as_of`, of every action on any of `isins`, by ex-date.

    A later version (a new status, revised terms, a human-verified ratio) is a
    new row with the same `action_key`: the newest is the latest `as_of`, then
    the highest id. The newest version decides the ISIN, so a correction that
    moves an action to another ISIN moves it here too.
    """
    require_aware(as_of, "as_of")
    ca = CorporateAction
    keys = select(ca.action_key).where(ca.isin.in_(list(isins)), ca.as_of <= as_of)
    stmt = select(ca).where(ca.action_key.in_(keys), ca.as_of <= as_of)
    newest = _newest_per_key(session.scalars(stmt))
    return [
        row
        for row in newest
        if row.isin in isins and (action_types is None or row.action_type in action_types)
    ]


def _usable(row: CorporateAction, on: date) -> str | None:
    """Why this version adjusts nothing on `on`, or None if it does."""
    if row.status is CorporateActionStatus.WITHDRAWN:
        return "withdrawn"
    if row.status in _PRELIMINARY or row.ex_date is None:
        return f"status {row.status}: terms not final"
    if row.ratio_basis not in _ADJUSTING_BASES:
        return f"ratio basis {row.ratio_basis}: needs human verification"
    if row.ex_date > on:
        return f"ex-date {row.ex_date} is after {on}"
    return None


def isin_lineage_as_of(session: Session, *, isin: str, as_of: datetime) -> list[str]:
    """`isin` followed by every ISIN it replaced, through `isin_change` actions known and effective by `as_of`.

    Computed, never stored: the entity table keeps one row per ISIN, and ISIN
    change evidence may arrive out of order (docs/temporal-model.md). Newest
    first; a chain that loops is cut where it repeats.
    """
    require_aware(as_of, "as_of")
    on = as_of.astimezone(IST).date()
    ca = CorporateAction
    changes = _newest_per_key(
        session.scalars(
            select(ca).where(ca.action_type == CorporateActionType.ISIN_CHANGE, ca.as_of <= as_of)
        )
    )
    predecessors: dict[str, list[str]] = {}
    for row in changes:
        if _usable(row, on) is None and row.new_isin is not None:
            predecessors.setdefault(row.new_isin, []).append(row.isin)
    lineage, frontier = [isin], [isin]
    while frontier:
        nxt = []
        for current in frontier:
            for old in sorted(predecessors.get(current, [])):
                if old not in lineage:
                    lineage.append(old)
                    nxt.append(old)
        frontier = nxt
    return lineage


@dataclass(frozen=True)
class AppliedAdjustment:
    action: CorporateAction
    factor: PriceFactor
    cum_close: Decimal | None = None  # rights only: the close the TERP was computed from


@dataclass(frozen=True)
class SkippedAdjustment:
    action: CorporateAction
    reason: str


@dataclass(frozen=True)
class PriceAdjustments:
    """The adjustment factors known and effective at `as_of`, and every capital action left out, with why."""

    as_of: datetime
    lineage: tuple[str, ...]
    applied: tuple[AppliedAdjustment, ...]
    skipped: tuple[SkippedAdjustment, ...]

    @property
    def factors(self) -> list[PriceFactor]:
        return [a.factor for a in self.applied]


def _cum_close(session: Session, *, isin: str, ex_date: date, as_of: datetime) -> Decimal | None:
    """The last close before `ex_date`: exchange bhavcopy first, then Upstox."""
    start, end = ex_date - CUM_CLOSE_LOOKBACK, ex_date - timedelta(days=1)
    for rows in (
        bhavcopy_rows_as_of(session, isin=isin, start=start, end=end, as_of=as_of),
        upstox_candles_as_of(session, isin=isin, start=start, end=end, as_of=as_of),
    ):
        if rows:
            return rows[-1].close
    return None


def _factor(session: Session, row: CorporateAction, as_of: datetime) -> AppliedAdjustment | str:
    kind, ex_date = row.action_type, row.ex_date
    assert ex_date is not None  # _usable checked it
    if kind in (CorporateActionType.SPLIT, CorporateActionType.CONSOLIDATION):
        return AppliedAdjustment(row, PriceFactor(ex_date, split_factor(row.face_value_from, row.face_value_to)))
    if kind is CorporateActionType.BONUS:
        return AppliedAdjustment(row, PriceFactor(ex_date, bonus_factor(row.shares_new, row.shares_held)))
    if kind is CorporateActionType.DEMERGER:
        return AppliedAdjustment(row, PriceFactor(ex_date, demerger_factor(row.retained_fraction)))
    cum = _cum_close(session, isin=row.isin, ex_date=ex_date, as_of=as_of)
    if cum is None or not cum > 0:
        return f"no close in the {CUM_CLOSE_LOOKBACK.days} days before the rights ex-date {ex_date}"
    factor = rights_factor(row.shares_new, row.shares_held, row.issue_price, cum)
    return AppliedAdjustment(row, PriceFactor(ex_date, factor), cum_close=cum)


def price_adjustments_as_of(session: Session, *, isin: str, as_of: datetime) -> PriceAdjustments:
    """Price adjustment factors for `isin` and the ISINs it replaced, as known and effective at `as_of`.

    Only capital actions adjust prices (splits, consolidations, bonuses,
    rights, demergers); dividends never do. An action counts only if its
    newest version known by `as_of` is final (not announced, approved or
    withdrawn), its ex-date is on or before `as_of` (IST date), and its terms
    came from an exchange field or were verified by a named person: a ratio
    read from a PDF adjusts nothing until verified. Everything left out is
    returned in `skipped` with the reason, never silently dropped.
    """
    require_aware(as_of, "as_of")
    on = as_of.astimezone(IST).date()
    lineage = isin_lineage_as_of(session, isin=isin, as_of=as_of)
    applied: list[AppliedAdjustment] = []
    skipped: list[SkippedAdjustment] = []
    for row in corporate_actions_as_of(session, isins=lineage, as_of=as_of, action_types=CAPITAL_ACTION_TYPES):
        reason = _usable(row, on)
        result = reason if reason is not None else _factor(session, row, as_of)
        if isinstance(result, str):
            skipped.append(SkippedAdjustment(row, result))
        else:
            applied.append(result)
    return PriceAdjustments(as_of, tuple(lineage), tuple(applied), tuple(skipped))


def adjusted_closes_as_of(
    session: Session, *, isin: str, start: date, end: date, as_of: datetime
) -> dict[date, Decimal]:
    """NSE bhavcopy closes for `isin` and the ISINs it replaced, adjusted with the factors known at `as_of`.

    Bhavcopy prices are as traded, so every factor applies. Upstox candles are
    not used here: whether the vendor has already adjusted them is checked by
    upstox_bhavcopy_crosscheck_as_of, never assumed. A date on which two
    lineage ISINs both traded keeps the newer ISIN's close.
    """
    adjustments = price_adjustments_as_of(session, isin=isin, as_of=as_of)
    closes: dict[date, Decimal] = {}
    for member in reversed(adjustments.lineage):  # oldest first: the newer ISIN wins a shared date
        for row in bhavcopy_rows_as_of(session, isin=member, start=start, end=end, as_of=as_of):
            closes[row.trade_date] = row.close
    return adjust_series(closes, adjustments.factors)


def corporate_action_file_rows_as_of(
    session: Session, *, content_hash: str, extracted_by: str, rule_version: str, as_of: datetime
) -> tuple[set[tuple[str, datetime]], set[tuple[int | None, str, str]]]:
    """What one file already produced under `rule_version`: action (key, as_of) and quarantine (row, reason, detail).

    The loader's backstop, so a rerun or reparse writes only what is new
    (e.g. a row whose symbol has since become resolvable).
    """
    require_aware(as_of, "as_of")
    ca, q = CorporateAction, CorporateActionQuarantine
    actions = set(
        session.execute(
            select(ca.action_key, ca.as_of).where(
                ca.content_hash == content_hash, ca.extracted_by == extracted_by,
                ca.rule_version == rule_version, ca.as_of <= as_of,
            )  # fmt: skip
        ).tuples()
    )
    issues = set(
        session.execute(
            select(q.row_number, q.reason, q.detail).where(
                q.content_hash == content_hash, q.extracted_by == extracted_by,
                q.rule_version == rule_version, q.as_of <= as_of,
            )  # fmt: skip
        ).tuples()
    )
    return actions, {(n, str(reason), detail) for n, reason, detail in issues}


def corporate_action_quarantine_as_of(session: Session, *, as_of: datetime) -> list[CorporateActionQuarantine]:
    """Corporate-action files and rows held back for review, known by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = CorporateActionQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class CorporateActionQuarantineEntry:
    row: CorporateActionQuarantine
    resolved: bool

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def corporate_action_quarantine_review_as_of(
    session: Session, *, current_rule_version: str, as_of: datetime
) -> list[CorporateActionQuarantineEntry]:
    """Every corporate-action quarantine row known by `as_of`, marked once it has been dealt with.

    A file row is identified by (content_hash, row_number). An entry is
    resolved when that row has since produced an action (e.g. its symbol
    became resolvable), or when it is under an older rule, the file has been
    parsed under `current_rule_version`, and that parse did not quarantine
    the same row again. Quarantine rows are never deleted (append-only).
    """
    rows = corporate_action_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    ca = CorporateAction
    hashes = {row.content_hash for row in rows}
    produced = set(
        session.execute(
            select(ca.content_hash, ca.source_row, ca.rule_version)
            .where(ca.content_hash.in_(hashes), ca.as_of <= as_of)
            .distinct()
        ).tuples()
    )
    parsed_now = {(h, v) for h, _, v in produced if v == current_rule_version}
    produced_rows = {(h, n) for h, n, _ in produced}
    requarantined = set()
    for row in rows:
        if row.rule_version == current_rule_version:
            parsed_now.add((row.content_hash, row.rule_version))
            requarantined.add((row.content_hash, row.row_number))
    entries = []
    for row in rows:
        file_row = (row.content_hash, row.row_number)
        resolved = file_row in produced_rows or (
            row.rule_version != current_rule_version
            and (row.content_hash, current_rule_version) in parsed_now
            and file_row not in requarantined
        )
        entries.append(CorporateActionQuarantineEntry(row, resolved))
    return entries


# --------------------------------------------------------------------------- #
# XBRL results filings (financial_facts)
# --------------------------------------------------------------------------- #


def financial_filings_as_of(
    session: Session, *, isin: str | None = None, as_of: datetime
) -> list[FinancialFiling]:
    """Parsed XBRL results files known by `as_of`, oldest first; every rule_version's row."""
    require_aware(as_of, "as_of")
    f = FinancialFiling
    stmt = select(f).where(f.as_of <= as_of).order_by(f.as_of, f.id)
    if isin is not None:
        stmt = stmt.where(f.isin == isin)
    return list(session.scalars(stmt))


def financial_filing_loaded_as_of(
    session: Session, *, content_hash: str, file_as_of: datetime, rule_version: str, as_of: datetime
) -> tuple[bool, set[tuple[str, str]]]:
    """Whether one file was parsed into a filing under `rule_version`, and its whole-file quarantines.

    The loader's backstop: a parsed file is skipped; a whole-file quarantine
    (reason, detail) is not written twice, but the file is tried again, so an
    ISIN that has since become resolvable loads.
    """
    require_aware(as_of, "as_of")
    f, q = FinancialFiling, FinancialFactsQuarantine
    parsed = session.scalar(
        select(f.id).where(
            f.content_hash == content_hash, f.as_of == file_as_of, f.rule_version == rule_version, f.as_of <= as_of
        ).limit(1)
    )
    rejected = session.execute(
        select(q.reason, q.detail).where(
            q.content_hash == content_hash, q.as_of == file_as_of, q.rule_version == rule_version,
            q.xbrl_element.is_(None), q.as_of <= as_of,
        )  # fmt: skip
    ).tuples()
    return parsed is not None, {(str(reason), detail) for reason, detail in rejected}


def financial_facts_quarantine_as_of(session: Session, *, as_of: datetime) -> list[FinancialFactsQuarantine]:
    """XBRL files and facts held back for review, known by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = FinancialFactsQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class FinancialFactsQuarantineEntry:
    row: FinancialFactsQuarantine
    resolved: bool

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def financial_facts_quarantine_review_as_of(
    session: Session, *, current_rule_version: str, as_of: datetime
) -> list[FinancialFactsQuarantineEntry]:
    """Every XBRL quarantine row known by `as_of`, marked once it has been dealt with.

    A whole-file entry is resolved when that file (content_hash, as_of) has
    since produced a filing under the entry's own rule (e.g. its ISIN became
    resolvable) or under `current_rule_version`. A fact entry is resolved when it
    is under an older rule, the file has been parsed under
    `current_rule_version`, and that parse did not quarantine the same
    (element, context) again. Quarantine rows are never deleted (append-only).
    """
    rows = financial_facts_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    f = FinancialFiling
    hashes = {row.content_hash for row in rows}
    filings = set(
        session.execute(
            select(f.content_hash, f.as_of, f.rule_version)
            .where(f.content_hash.in_(hashes), f.as_of <= as_of)
            .distinct()
        ).tuples()
    )
    parsed_under = filings
    parsed_now = {(h, t) for h, t, v in filings if v == current_rule_version}
    requarantined = {
        (row.content_hash, row.as_of, row.xbrl_element, row.context_ref)
        for row in rows
        if row.rule_version == current_rule_version and row.xbrl_element is not None
    }
    entries = []
    for row in rows:
        file = (row.content_hash, row.as_of)
        if row.xbrl_element is None:
            resolved = (*file, row.rule_version) in parsed_under or file in parsed_now
        else:
            resolved = (
                row.rule_version != current_rule_version
                and file in parsed_now
                and (*file, row.xbrl_element, row.context_ref) not in requarantined
            )
        entries.append(FinancialFactsQuarantineEntry(row, resolved))
    return entries


# --------------------------------------------------------------------------- #
# Ratios computed from financial_facts at read time
# --------------------------------------------------------------------------- #


class RatioGap(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # an input came from a filing family the rule does not cover
    MISSING_INPUT = "missing_input"
    UNDEFINED = "undefined"  # a denominator is zero or negative
    INVALID_INPUT = "invalid_input"  # e.g. negative finance costs or borrowings


@dataclass(frozen=True)
class RatioValue:
    """One ratio for one period, with the facts it was computed from.

    `value` is None exactly when `gap` is set. `missing` names each absent
    input as "line_item start..end" (instant: "line_item @end").
    """

    ratio: ratios.Ratio
    period_start: date | None  # None for a balance-sheet ratio
    period_end: date
    value: Decimal | None
    gap: RatioGap | None
    inputs: tuple[FinancialFact, ...]
    missing: tuple[str, ...] = ()
    rule_version: str = ratios.RULE_VERSION

    @property
    def known_since(self) -> datetime | None:
        """When the last input became known; the ratio could not be computed earlier."""
        return max((f.as_of for f in self.inputs), default=None)


_FactKey = tuple[str, date | None, date]  # (line_item, period_start, period_end)


def _fact_label(key: _FactKey) -> str:
    item, start, end = key
    return f"{item} @{end}" if start is None else f"{item} {start}..{end}"


def ratios_as_of(
    session: Session, *, isin: str, consolidation: Consolidation, as_of: datetime
) -> list[RatioValue]:
    """Every ratio core.compute.ratios defines, for every period the facts known at `as_of` cover.

    Margins and interest cover for every reported duration (quarter, year to
    date, year); debt ratios for every balance-sheet date; ROCE and
    incremental ROCE for fiscal years. Nothing is stored: a restatement known
    later changes the ratio only for reads at or after it. Oldest period first.
    """
    facts = facts_as_of(
        session, isin=isin, consolidation=consolidation, as_of=as_of,
        line_items=ratios.DURATION_LINE_ITEMS + ratios.INSTANT_LINE_ITEMS,
    )  # fmt: skip
    by_key: dict[_FactKey, FinancialFact] = {(f.line_item, f.period_start, f.period_end): f for f in facts}
    f = FinancialFiling
    families: dict[str, str] = {
        digest: taxonomy
        for digest, taxonomy in session.execute(
            select(f.content_hash, f.taxonomy)
            .where(f.content_hash.in_({x.content_hash for x in facts}), f.as_of <= as_of)
            .distinct()
        ).tuples()
    }

    def compute(ratio, start, end, needed: dict[str, _FactKey], formula) -> RatioValue:
        found = {name: by_key[key] for name, key in needed.items() if key in by_key}
        inputs = tuple(found.values())
        missing = tuple(_fact_label(key) for name, key in needed.items() if name not in found)
        if any(families.get(x.content_hash) not in ratios.APPLICABLE_FAMILIES for x in inputs):
            return RatioValue(ratio, start, end, None, RatioGap.NOT_APPLICABLE, inputs, missing)
        if missing:
            return RatioValue(ratio, start, end, None, RatioGap.MISSING_INPUT, inputs, missing)
        try:
            value = formula(**{name: x.value for name, x in found.items()})
        except ValueError:
            return RatioValue(ratio, start, end, None, RatioGap.INVALID_INPUT, inputs)
        return RatioValue(ratio, start, end, value, None if value is not None else RatioGap.UNDEFINED, inputs)

    def duration(start: date, end: date, *items: str) -> dict[str, _FactKey]:
        return {item: (item, start, end) for item in items}

    def instant(end: date, *items: str, suffix: str = "") -> dict[str, _FactKey]:
        return {item + suffix: (item, None, end) for item in items}

    r = ratios
    out: list[RatioValue] = []
    durations = sorted({(s, e) for (item, s, e) in by_key if s is not None})
    balance_dates = sorted({e for (item, s, e) in by_key if s is None})

    for start, end in durations:
        pnl = duration(start, end, r.REVENUE, r.TOTAL_EXPENSES, r.FINANCE_COSTS, r.DEPRECIATION)
        out.append(compute(
            r.Ratio.OPERATING_MARGIN, start, end, pnl,
            lambda **v: r.margin(r.operating_profit(
                v[r.REVENUE], v[r.TOTAL_EXPENSES], v[r.FINANCE_COSTS], v[r.DEPRECIATION]
            ), v[r.REVENUE]),
        ))  # fmt: skip
        out.append(compute(
            r.Ratio.EBIT_MARGIN, start, end, duration(start, end, r.PROFIT_BEFORE_TAX, r.FINANCE_COSTS, r.REVENUE),
            lambda **v: r.margin(r.ebit(v[r.PROFIT_BEFORE_TAX], v[r.FINANCE_COSTS]), v[r.REVENUE]),
        ))  # fmt: skip
        out.append(compute(
            r.Ratio.NET_MARGIN, start, end, duration(start, end, r.PROFIT, r.REVENUE),
            lambda **v: r.margin(v[r.PROFIT], v[r.REVENUE]),
        ))  # fmt: skip
        out.append(compute(
            r.Ratio.INTEREST_COVERAGE, start, end, duration(start, end, r.PROFIT_BEFORE_TAX, r.FINANCE_COSTS),
            lambda **v: r.interest_coverage(r.ebit(v[r.PROFIT_BEFORE_TAX], v[r.FINANCE_COSTS]), v[r.FINANCE_COSTS]),
        ))  # fmt: skip

    def borrowings(v: dict[str, Decimal], suffix: str = "") -> Decimal:
        return r.total_borrowings(v[r.BORROWINGS_CURRENT + suffix], v[r.BORROWINGS_NONCURRENT + suffix])

    def capital(v: dict[str, Decimal], suffix: str = "") -> Decimal:
        return r.capital_employed(v[r.EQUITY + suffix], borrowings(v, suffix))

    capital_items = (r.EQUITY, r.BORROWINGS_CURRENT, r.BORROWINGS_NONCURRENT)
    for end in balance_dates:
        out.append(compute(
            r.Ratio.DEBT_TO_EQUITY, None, end, instant(end, *capital_items),
            lambda **v: r.debt_to_equity(borrowings(v), v[r.EQUITY]),
        ))  # fmt: skip
        out.append(compute(
            r.Ratio.NET_DEBT_TO_EQUITY, None, end,
            instant(end, *capital_items, r.CASH, r.OTHER_BANK_BALANCES, r.CURRENT_INVESTMENTS),
            lambda **v: r.debt_to_equity(
                r.net_debt(borrowings(v), v[r.CASH], v[r.OTHER_BANK_BALANCES], v[r.CURRENT_INVESTMENTS]),
                v[r.EQUITY],
            ),
        ))  # fmt: skip

    for start, end in durations:
        if not r.is_fiscal_year(start, end):
            continue
        earnings = duration(start, end, r.PROFIT_BEFORE_TAX, r.FINANCE_COSTS)
        out.append(compute(
            r.Ratio.ROCE, start, end,
            earnings | instant(end, *capital_items) | instant(r.prior_balance_date(start), *capital_items, suffix="@open"),
            lambda **v: r.roce(
                r.ebit(v[r.PROFIT_BEFORE_TAX], v[r.FINANCE_COSTS]),
                r.average_balance(capital(v, "@open"), capital(v)),
            ),
        ))  # fmt: skip
        years = r.INCREMENTAL_ROCE_YEARS
        before_start, before_end = r.shift_years(start, -years), r.shift_years(end, -years)
        before = {
            k + "@before": key
            for k, key in (duration(before_start, before_end, r.PROFIT_BEFORE_TAX, r.FINANCE_COSTS)
                           | instant(before_end, *capital_items)).items()
        }  # fmt: skip
        out.append(compute(
            r.Ratio.INCREMENTAL_ROCE, start, end, earnings | instant(end, *capital_items) | before,
            lambda **v: r.incremental_roce(
                r.ebit(v[r.PROFIT_BEFORE_TAX], v[r.FINANCE_COSTS]),
                r.ebit(v[r.PROFIT_BEFORE_TAX + "@before"], v[r.FINANCE_COSTS + "@before"]),
                capital(v),
                capital(v, "@before"),
            ),
        ))  # fmt: skip

    return sorted(out, key=lambda x: (x.period_end, x.period_start or x.period_end, x.ratio))


# --------------------------------------------------------------------------- #
# Shareholding-pattern filings (shareholding_pattern)
# --------------------------------------------------------------------------- #


def shareholding_filings_as_of(
    session: Session, *, isin: str | None = None, as_of: datetime
) -> list[ShareholdingFiling]:
    """Parsed shareholding-pattern files known by `as_of`, oldest first; every rule_version's row."""
    require_aware(as_of, "as_of")
    f = ShareholdingFiling
    stmt = select(f).where(f.as_of <= as_of).order_by(f.as_of, f.id)
    if isin is not None:
        stmt = stmt.where(f.isin == isin)
    return list(session.scalars(stmt))


def shareholding_filing_loaded_as_of(
    session: Session, *, content_hash: str, file_as_of: datetime, rule_version: str, as_of: datetime
) -> tuple[bool, set[tuple[str, str]]]:
    """Whether one file was parsed into a filing under `rule_version`, and its whole-file quarantines.

    The loader's backstop, as for financial_filing_loaded_as_of.
    """
    require_aware(as_of, "as_of")
    f, q = ShareholdingFiling, ShareholdingQuarantine
    parsed = session.scalar(
        select(f.id).where(
            f.content_hash == content_hash, f.as_of == file_as_of, f.rule_version == rule_version, f.as_of <= as_of
        ).limit(1)
    )
    rejected = session.execute(
        select(q.reason, q.detail).where(
            q.content_hash == content_hash, q.as_of == file_as_of, q.rule_version == rule_version,
            q.xbrl_element.is_(None), q.as_of <= as_of,
        )  # fmt: skip
    ).tuples()
    return parsed is not None, {(str(reason), detail) for reason, detail in rejected}


def shareholding_pattern_as_of(
    session: Session, *, isin: str, as_of: datetime, as_on_date: date | None = None
) -> dict[date, list[ShareholdingPattern]]:
    """The shareholding pattern of `isin` for each "as on" date, as known at `as_of`.

    For each "as on" date the newest filing known at `as_of` wins: a revised
    filing (later as_of) replaces the original, and a reparse under a newer
    rule_version replaces the older parse. Only that filing's rows are
    returned, never a mix. Rows are ordered by category, then measure.
    """
    require_aware(as_of, "as_of")
    latest: dict[date, ShareholdingFiling] = {}
    for filing in shareholding_filings_as_of(session, isin=isin, as_of=as_of):
        if as_on_date is None or filing.as_on_date == as_on_date:
            latest[filing.as_on_date] = filing  # oldest first, so the last one wins
    p = ShareholdingPattern
    out: dict[date, list[ShareholdingPattern]] = {}
    for day, filing in sorted(latest.items()):
        out[day] = list(
            session.scalars(
                select(p)
                .where(
                    p.content_hash == filing.content_hash, p.as_of == filing.as_of,
                    p.rule_version == filing.rule_version, p.isin == isin, p.as_of <= as_of,
                )  # fmt: skip
                .order_by(p.category, p.measure)
            )
        )
    return out


def shareholding_quarantine_as_of(session: Session, *, as_of: datetime) -> list[ShareholdingQuarantine]:
    """Shareholding files and facts held back for review, known by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = ShareholdingQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class ShareholdingQuarantineEntry:
    row: ShareholdingQuarantine
    resolved: bool

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def shareholding_quarantine_review_as_of(
    session: Session, *, current_rule_version: str, as_of: datetime
) -> list[ShareholdingQuarantineEntry]:
    """Every shareholding quarantine row known by `as_of`, marked once it has been dealt with.

    Same rules as financial_facts_quarantine_review_as_of: a whole-file entry
    is resolved once that file has produced a filing under the entry's rule or
    `current_rule_version`; a fact entry once the file was reparsed under
    `current_rule_version` without quarantining the same (element, context).
    """
    rows = shareholding_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    f = ShareholdingFiling
    hashes = {row.content_hash for row in rows}
    filings = set(
        session.execute(
            select(f.content_hash, f.as_of, f.rule_version)
            .where(f.content_hash.in_(hashes), f.as_of <= as_of)
            .distinct()
        ).tuples()
    )
    parsed_now = {(h, t) for h, t, v in filings if v == current_rule_version}
    requarantined = {
        (row.content_hash, row.as_of, row.xbrl_element, row.context_ref)
        for row in rows
        if row.rule_version == current_rule_version and row.xbrl_element is not None
    }
    entries = []
    for row in rows:
        file = (row.content_hash, row.as_of)
        if row.xbrl_element is None:
            resolved = (*file, row.rule_version) in filings or file in parsed_now
        else:
            resolved = (
                row.rule_version != current_rule_version
                and file in parsed_now
                and (*file, row.xbrl_element, row.context_ref) not in requarantined
            )
        entries.append(ShareholdingQuarantineEntry(row, resolved))
    return entries


# --------------------------------------------------------------------------- #
# Screener exports (validation reference, never an input)
# --------------------------------------------------------------------------- #


def screener_export_loaded_as_of(
    session: Session, *, content_hash: str, file_as_of: datetime, rule_version: str, as_of: datetime
) -> ScreenerExport | None:
    """The export row parsed from one stored file under `rule_version`, if any, known by `as_of`."""
    require_aware(as_of, "as_of")
    e = ScreenerExport
    return session.scalar(
        select(e).where(
            e.content_hash == content_hash, e.as_of == file_as_of, e.rule_version == rule_version, e.as_of <= as_of
        ).order_by(e.id).limit(1)
    )


def screener_exports_as_of(session: Session, *, as_of: datetime, isin: str | None = None) -> list[ScreenerExport]:
    """Parsed Screener exports known by `as_of`, oldest first; every rule_version's row."""
    require_aware(as_of, "as_of")
    e = ScreenerExport
    stmt = select(e).where(e.as_of <= as_of).order_by(e.as_of, e.id)
    if isin is not None:
        stmt = stmt.where(e.isin == isin)
    return list(session.scalars(stmt))


def screener_values_as_of(
    session: Session, *, isin: str, consolidation: Consolidation, as_of: datetime, rule_version: str
) -> tuple[ScreenerExport | None, list[ScreenerValue]]:
    """The newest export of `isin` known at `as_of` under `rule_version`, and its values.

    Values are never mixed across exports: one export is one consistent view
    of Screener at its export time.
    """
    require_aware(as_of, "as_of")
    e, v = ScreenerExport, ScreenerValue
    export = session.scalar(
        select(e)
        .where(e.isin == isin, e.consolidation == consolidation, e.rule_version == rule_version, e.as_of <= as_of)
        .order_by(e.as_of.desc(), e.id.desc())
        .limit(1)
    )
    if export is None:
        return None, []
    values = session.scalars(
        select(v)
        .where(
            v.content_hash == export.content_hash, v.as_of == export.as_of, v.rule_version == rule_version,
            v.isin == isin, v.consolidation == consolidation,
        )  # fmt: skip
        .order_by(v.statement, v.line, v.period_end)
    )
    return export, list(values)


# --------------------------------------------------------------------------- #
# Guidance claims from concall transcripts (guidance_claim)
# --------------------------------------------------------------------------- #


def concall_documents_as_of(
    session: Session, *, isin: str | None = None, as_of: datetime
) -> list[ConcallDocument]:
    """Transcript files turned into text by `as_of`, oldest first; every rule_version's row."""
    require_aware(as_of, "as_of")
    d = ConcallDocument
    stmt = select(d).where(d.as_of <= as_of).order_by(d.as_of, d.id)
    if isin is not None:
        stmt = stmt.where(d.isin == isin)
    return list(session.scalars(stmt))


def concall_document_loaded_as_of(
    session: Session, *, content_hash: str, file_as_of: datetime, rule_version: str, as_of: datetime
) -> tuple[bool, set[tuple[str, str]]]:
    """Whether one transcript file was turned into text under `rule_version`, and why it was held back.

    The loader's backstop, as for shareholding_filing_loaded_as_of.
    """
    require_aware(as_of, "as_of")
    d, q = ConcallDocument, GuidanceQuarantine
    parsed = session.scalar(
        select(d.id).where(
            d.content_hash == content_hash, d.as_of == file_as_of, d.rule_version == rule_version,
            d.as_of <= as_of,
        ).limit(1)  # fmt: skip
    )
    rejected = session.execute(
        select(q.reason, q.detail).where(
            q.content_hash == content_hash, q.as_of == file_as_of, q.rule_version == rule_version,
            q.quote.is_(None), q.extractor_version.is_(None), q.as_of <= as_of,
        )  # fmt: skip
    ).tuples()
    return parsed is not None, {(str(reason), detail) for reason, detail in rejected}


def concall_transcripts_as_of(
    session: Session, *, isin: str | None = None, as_of: datetime
) -> list[ConcallTranscript]:
    """Extracted transcripts known by `as_of`, oldest first; every extraction's row.

    A document re-extracted under a newer model or a grown hedge lexicon has
    several rows at the same `as_of`. They are all returned: which one a caller
    wants is its own decision, and `guidance_claims_as_of` states the one this
    module makes.
    """
    require_aware(as_of, "as_of")
    t = ConcallTranscript
    stmt = select(t).where(t.as_of <= as_of).order_by(t.as_of, t.id)
    if isin is not None:
        stmt = stmt.where(t.isin == isin)
    return list(session.scalars(stmt))


def concall_transcript_loaded_as_of(
    session: Session,
    *,
    content_hash: str,
    file_as_of: datetime,
    extractor_version: str,
    model_requested: str | None = None,
    prompt_hash: str | None = None,
    as_of: datetime,
) -> tuple[bool, set[tuple[str, str]]]:
    """Whether one transcript was already extracted under this setup, and why it was held back.

    The extractor's backstop, as for shareholding_filing_loaded_as_of, and the
    reason a re-run costs nothing: an unchanged document, model, prompt and
    extractor has nothing new to say, so it is not sent again. `model_version`
    `model_requested` is the tier as asked for, not the weights that answered:
    it is the only one of the two the caller knows before the call. It and
    `prompt_hash` left out mean "under any".
    """
    require_aware(as_of, "as_of")
    t, q = ConcallTranscript, GuidanceQuarantine
    stmt = select(t.id).where(
        t.content_hash == content_hash, t.as_of == file_as_of, t.as_of <= as_of,
        t.extractor_version == extractor_version,
    )  # fmt: skip
    if model_requested is not None:
        stmt = stmt.where(t.model_requested == model_requested)
    if prompt_hash is not None:
        stmt = stmt.where(t.prompt_hash == prompt_hash)
    extracted = session.scalar(stmt.limit(1))
    rejected = session.execute(
        select(q.reason, q.detail).where(
            q.content_hash == content_hash, q.as_of == file_as_of, q.as_of <= as_of,
            q.extractor_version == extractor_version, q.quote.is_(None),
        )  # fmt: skip
    ).tuples()
    return extracted is not None, {(str(reason), detail) for reason, detail in rejected}


def _current_extractions(rows: Iterable[ConcallTranscript]) -> dict[tuple[str, datetime], int]:
    """The extraction that stands for each document: the last one loaded.

    Rows arrive oldest first, so the last one wins -- the same rule
    shareholding_pattern_as_of uses for a revised filing. A re-extraction is
    stored at the document's original `as_of` (R2), so `as_of` cannot separate
    two runs over the same bytes; load order can.
    """
    current: dict[tuple[str, datetime], int] = {}
    for row in rows:
        current[(row.content_hash, row.as_of)] = row.id
    return current


def guidance_claims_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
    metric: str | None = None,
    sections: Collection[ClaimSection] | None = None,
) -> list[GuidanceClaim]:
    """Guidance `isin` had given by `as_of`, oldest first.

    Only claims from the extraction that stands for each document (see
    `_current_extractions`); an earlier run's claims over the same bytes stay
    in the store but are not read as current.
    """
    require_aware(as_of, "as_of")
    transcripts = concall_transcripts_as_of(session, isin=isin, as_of=as_of)
    current = _current_extractions(transcripts)
    if not current:
        return []
    by_id = {row.id: row for row in transcripts}
    keys = {
        (row.content_hash, row.as_of, row.extractor_version, row.model_version, row.prompt_hash)
        for row in (by_id[i] for i in current.values())
    }
    c = GuidanceClaim
    stmt = select(c).where(c.isin == isin, c.as_of <= as_of).order_by(c.as_of, c.call_date, c.quote_start, c.id)
    if metric is not None:
        stmt = stmt.where(c.metric == metric)
    if sections is not None:
        stmt = stmt.where(c.section.in_(list(sections)))
    return [
        row
        for row in session.scalars(stmt)
        if (row.content_hash, row.as_of, row.extractor_version, row.model_version, row.prompt_hash) in keys
    ]


@dataclass(frozen=True)
class GuidanceDirection:
    """One claim read against the last comparable one before it.

    Not a stored column. Which earlier claim is comparable depends on what was
    known at `as_of`, so a stored answer would freeze one reading of history
    and quietly become look-ahead (R2). `previous` is None for the first claim
    on a metric and period, where `direction` is NEW.
    """

    current: GuidanceClaim
    previous: GuidanceClaim | None
    direction: Direction
    hedge_change: HedgeChange | None


def _comparable(row: GuidanceClaim) -> ComparableClaim:
    return ComparableClaim(
        metric=row.metric,
        period_label=row.period_label,
        value=ParsedValue(
            low=row.value_low, high=row.value_high, unit=Unit(row.value_unit), specificity=Specificity(row.specificity)
        ),
        hedge=HedgeStrength(row.hedge_strength),
    )


def guidance_directions_as_of(
    session: Session, *, isin: str, as_of: datetime, metric: str | None = None
) -> list[GuidanceDirection]:
    """Whether each claim raised, lowered, maintained or reshaped the one before it.

    Computed here by core/compute/guidance.py, never extracted and never stored
    (R1). Claims are chained within a (metric, period_label): the previous
    claim is the newest one on the same metric and period known before this
    one. WITHDRAWN is not produced here -- a promise that stops being made is
    silence, and silence is detected by looking at the calls that followed, not
    at one claim.
    """
    chains: dict[tuple[str, str], GuidanceClaim] = {}
    out: list[GuidanceDirection] = []
    for row in guidance_claims_as_of(session, isin=isin, as_of=as_of, metric=metric):
        key = (row.metric, row.period_label)
        previous = chains.get(key)
        direction = compare_claims(None if previous is None else _comparable(previous), _comparable(row))
        hedge_change = (
            None
            if previous is None
            else compare_hedges(HedgeStrength(previous.hedge_strength), HedgeStrength(row.hedge_strength))
        )
        out.append(GuidanceDirection(current=row, previous=previous, direction=direction, hedge_change=hedge_change))
        chains[key] = row
    return out


# --------------------------------------------------------------------------- #
# Guidance graded against what was reported, and what management stopped saying
# --------------------------------------------------------------------------- #

# Every line item any resolver reads: the only facts worth fetching.
_RESOLVER_LINE_ITEMS = tuple(
    sorted({need.line_item for resolver in METRIC_RESOLVERS.values() for need in resolver.needs})
)


def _resolver_facts(session: Session, *, isin: str, as_of: datetime) -> list[Fact]:
    """The Ind AS line items the resolvers read, one current version per key, as known at `as_of`.

    Both bases are returned, each fact tagged with its own; resolve_claim picks
    one. Only facts from a filing whose taxonomy is in ratios.APPLICABLE_FAMILIES
    are kept, exactly as ratios_as_of does: a bank or an insurer files other
    line items, and a same-named element in another family must not be graded
    against. A fact whose filing is not known at `as_of` is dropped with them.
    """
    rows = [
        row
        for consolidation in Consolidation
        for row in facts_as_of(
            session, isin=isin, consolidation=consolidation, as_of=as_of, line_items=_RESOLVER_LINE_ITEMS
        )
    ]
    if not rows:
        return []
    f = FinancialFiling
    families = {
        digest: taxonomy
        for digest, taxonomy in session.execute(
            select(f.content_hash, f.taxonomy)
            .where(f.isin == isin, f.content_hash.in_({row.content_hash for row in rows}), f.as_of <= as_of)
            .distinct()
        ).tuples()
    }
    return [
        Fact(
            line_item=row.line_item,
            period_start=row.period_start,
            period_end=row.period_end,
            value=row.value,
            basis=Basis(row.consolidation.value),
        )
        for row in rows
        if families.get(row.content_hash) in ratios.APPLICABLE_FAMILIES
    ]


def _files_only_outside_ind_as(session: Session, *, isin: str, as_of: datetime) -> bool:
    """Whether every filing of `isin` known at `as_of` is of a family the resolvers do not read."""
    f = FinancialFiling
    taxonomies = set(session.scalars(select(f.taxonomy).where(f.isin == isin, f.as_of <= as_of).distinct()))
    return bool(taxonomies) and not taxonomies & ratios.APPLICABLE_FAMILIES


@dataclass(frozen=True)
class GuidanceResolution:
    """One claim and how it stands against what was reported by the read `as_of`.

    Not a stored column. Whether a claim is open, met or missed depends on
    which facts were public when it is read, so a stored answer would freeze
    one reading of history and quietly become look-ahead (R2).
    """

    claim: GuidanceClaim
    resolution: Resolution


def guidance_resolutions_as_of(
    session: Session, *, isin: str, as_of: datetime, metric: str | None = None
) -> list[GuidanceResolution]:
    """Whether each claim `isin` had made by `as_of` was met, missed, still open, or cannot be graded.

    Computed here by core/compute/guidance_resolution.py, never extracted and
    never stored (R1, R2). The grade depends on which facts were public at
    `as_of`: the same claim is OPEN before the results, MET or MISSED after
    them, and a restatement filed later changes the grade of a read made
    after it and of no read made before. Every read is filtered to rows with
    as_of at or before the read; nothing newer is looked at.

    Facts are the latest version per key at `as_of` (facts_as_of), Ind AS
    filings only. For ALREADY_KNOWN_WHEN_MADE the facts are read a second time
    as of the claim's own `as_of`, to see whether the answer already existed.
    Claims are those of the extraction that stands for each transcript
    (guidance_claims_as_of), so a re-extraction is not counted twice. Oldest
    first, every claim: a reiteration is graded on its own, and which ones
    feed a delivery rate is the choice of guidance_delivery_as_of.

    Data gaps, none repaired (R3): balance-sheet and cash-flow facts may not
    be stored for a quarter, so a claim on one stays OPEN. A company whose
    filings known at `as_of` are all of another taxonomy family (a bank, an
    NBFC, an insurer) has no Ind AS line items to grade against: an
    otherwise-OPEN claim is then UNRESOLVABLE with METRIC_NOT_IN_XBRL, since
    waiting will not help. Before it has filed anything, it is OPEN.
    """
    claims = guidance_claims_as_of(session, isin=isin, as_of=as_of, metric=metric)
    if not claims:
        return []
    facts = _resolver_facts(session, isin=isin, as_of=as_of)
    outside_ind_as = _files_only_outside_ind_as(session, isin=isin, as_of=as_of)
    when_made: dict[datetime, list[Fact]] = {as_of: facts}
    out: list[GuidanceResolution] = []
    for row in claims:
        if row.as_of not in when_made:
            when_made[row.as_of] = _resolver_facts(session, isin=isin, as_of=row.as_of)
        resolution = resolve_claim(_comparable(row), facts, facts_when_made=when_made[row.as_of])
        if outside_ind_as and resolution.status is Status.OPEN:
            resolution = replace(
                resolution, status=Status.UNRESOLVABLE, reason=UnresolvableReason.METRIC_NOT_IN_XBRL
            )
        out.append(GuidanceResolution(row, resolution))
    return out


@dataclass(frozen=True)
class GuidanceSilence:
    """One claim, its resolution, and whether management has since dropped it.

    Not a stored column, for the reason GuidanceResolution is not: silence is
    a fact about the calls public at the read `as_of`.
    """

    claim: GuidanceClaim
    resolution: Resolution
    silent: bool


def _with_silence(
    session: Session, *, isin: str, as_of: datetime, resolved: Sequence[GuidanceResolution]
) -> list[GuidanceSilence]:
    transcripts = concall_transcripts_as_of(session, isin=isin, as_of=as_of)
    current = _current_extractions(transcripts)
    by_id = {row.id: row for row in transcripts}
    said: dict[tuple[str, datetime], set[tuple[str, PeriodKey]]] = {key: set() for key in current}
    for item in resolved:
        claim = item.claim
        said[(claim.content_hash, claim.as_of)].add(claim_key(claim.metric, claim.period_label))
    calls = [
        TranscriptMentions(
            call_date=by_id[transcript_id].call_date,
            mentions=frozenset(said[key]),
            clean=by_id[transcript_id].claims_quarantined == 0,
        )
        for key, transcript_id in current.items()
    ]
    out = []
    for item in resolved:
        claim = item.claim
        metric, key = claim_key(claim.metric, claim.period_label)
        later = [call for call in calls if call.call_date > claim.call_date]
        silent = detect_silent(metric, key, later, resolved=period_resolved(item.resolution))
        out.append(GuidanceSilence(claim, item.resolution, silent))
    return out


def guidance_silence_as_of(session: Session, *, isin: str, as_of: datetime) -> list[GuidanceSilence]:
    """Which claims management had dropped by `as_of`: unsettled, and absent from the calls since.

    Computed here by core/compute/guidance_resolution.py, never extracted and
    never stored (R1, R2). A claim is silent once its period is not settled
    (see period_resolved) and each of the next rule.consecutive_calls later
    calls, all public at `as_of`, is clean and does not mention its metric
    and period. It is not silent before the last of those calls is published,
    whatever its call date: a read at t may not use a call filed after t. A
    call with quarantined claims cannot establish absence.

    Later means a call date strictly after the claim's own call. Each call is
    the extraction that stands for its transcript (see _current_extractions),
    so a re-extraction is not counted twice and the earlier run's claims do
    not count as mentions. Oldest claim first, as guidance_resolutions_as_of.
    """
    resolved = guidance_resolutions_as_of(session, isin=isin, as_of=as_of)
    return _with_silence(session, isin=isin, as_of=as_of, resolved=resolved)


def guidance_delivery_as_of(
    session: Session, *, isin: str, as_of: datetime, basis: Literal["first", "last"] = "first"
) -> DeliveryRate:
    """How much of what `isin` guided, by `as_of`, it delivered: the hedge-weighted share of graded claims met.

    Computed here by core/compute/guidance_resolution.py, never extracted and
    never stored (R1, R2): both the grades and the silence behind it depend on
    what was public at `as_of`, so a stored rate would be look-ahead by
    construction.

    One claim per (metric, period) feeds the rate: the first, the original
    promise, by default, or the last with basis="last" (select_claims says why
    the first is the default). Only MET and MISSED are in the rate. OPEN,
    UNRESOLVABLE and SILENT are counted apart and are not misses; whether
    silence should count against management is an open research question.
    """
    resolved = guidance_resolutions_as_of(session, isin=isin, as_of=as_of)
    rows = _with_silence(session, isin=isin, as_of=as_of, resolved=resolved)
    comparable = [_comparable(item.claim) for item in rows]
    by_claim = {id(c): item for c, item in zip(comparable, rows, strict=True)}
    chosen = select_claims(comparable, basis)
    return delivery_rate(
        ClaimOutcome(by_claim[id(c)].resolution.status, c.hedge, by_claim[id(c)].silent) for c in chosen
    )


def guidance_quarantine_as_of(session: Session, *, as_of: datetime) -> list[GuidanceQuarantine]:
    """Transcripts and claims held back for review, known by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    q = GuidanceQuarantine
    return list(session.scalars(select(q).where(q.as_of <= as_of).order_by(q.as_of, q.id)))


@dataclass(frozen=True)
class GuidanceQuarantineEntry:
    row: GuidanceQuarantine
    resolved: bool

    @property
    def needs_review(self) -> bool:
        return not self.resolved


def guidance_quarantine_review_as_of(
    session: Session, *, current_extractor_version: str, current_rule_version: str, as_of: datetime
) -> list[GuidanceQuarantineEntry]:
    """Every guidance quarantine row known by `as_of`, marked once it has been dealt with.

    A whole-document entry (`quote` NULL) is resolved once that document has
    produced a transcript row. A claim entry is resolved once the document was
    re-extracted under both current versions without holding the same quote
    back again -- so a hedge phrase added to the lexicon clears its backlog,
    and one that is still unmapped stays on the list.
    """
    rows = guidance_quarantine_as_of(session, as_of=as_of)
    if not rows:
        return []
    t = ConcallTranscript
    hashes = {row.content_hash for row in rows}
    extractions = set(
        session.execute(
            select(t.content_hash, t.as_of, t.extractor_version, t.rule_version)
            .where(t.content_hash.in_(hashes), t.as_of <= as_of)
            .distinct()
        ).tuples()
    )
    documents = {(h, t_) for h, t_, _, _ in extractions}
    extracted_now = {
        (h, t_)
        for h, t_, ev, rv in extractions
        if ev == current_extractor_version and rv == current_rule_version
    }
    requarantined = {
        (row.content_hash, row.as_of, row.quote)
        for row in rows
        if row.quote is not None
        and row.extractor_version == current_extractor_version
        and row.rule_version == current_rule_version
    }
    entries = []
    for row in rows:
        document = (row.content_hash, row.as_of)
        if row.quote is None:
            resolved = document in documents
        else:
            resolved = (
                (row.extractor_version, row.rule_version) != (current_extractor_version, current_rule_version)
                and document in extracted_now
                and (*document, row.quote) not in requarantined
            )
        entries.append(GuidanceQuarantineEntry(row, resolved))
    return entries


def guidance_report_as_of(
    session: Session, *, isin: str, symbol: str, as_of: datetime, quarter_start: date
) -> GuidanceReport:
    """The guidance report: what management promised, what landed, what went silent.

    Reads the guidance claims, resolutions, silence markers, and delivery rate
    at `as_of`, then assembles them into a GuidanceReport via build_report.
    Every fact contributing to an actual has its source (URL and as_of) looked
    up from the stored FinancialFact rows; if not found, source is None.
    """
    require_aware(as_of, "as_of")

    # Read all guidance data at as_of
    resolutions = guidance_resolutions_as_of(session, isin=isin, as_of=as_of)
    silence_items = _with_silence(session, isin=isin, as_of=as_of, resolved=resolutions)
    silence_map = {item.claim.id: item.silent for item in silence_items}
    delivery = guidance_delivery_as_of(session, isin=isin, as_of=as_of)

    # Build a map of (line_item, period_end) -> (source_url, source_as_of) from FinancialFact
    # Try consolidated first, then standalone if needed
    facts = facts_as_of(session, isin=isin, consolidation=Consolidation.CONSOLIDATED, as_of=as_of)
    if not facts:
        facts = facts_as_of(session, isin=isin, consolidation=Consolidation.STANDALONE, as_of=as_of)
    fact_sources: dict[tuple[str, date], tuple[str, datetime]] = {}
    for fact in facts:
        key = (fact.line_item, fact.period_end)
        fact_sources[key] = (fact.source_url, fact.as_of)

    # Build ClaimView for each resolution
    claims: list[ClaimView] = []
    for item in resolutions:
        claim = item.claim
        resolution = item.resolution
        silent = silence_map.get(claim.id, False)

        # Build InputFact for each input in the resolution
        inputs: list[InputFact] = []
        for line_item, period_end, value in resolution.inputs:
            source_key = (line_item, period_end)
            source_info = fact_sources.get(source_key)
            if source_info:
                source = SourceRef(url=source_info[0], as_of=source_info[1])
            else:
                source = None

            input_fact = InputFact(
                line_item=line_item,
                period_end=period_end,
                value=value,
                source=source,
            )
            inputs.append(input_fact)

        # Build SourceRef for the claim itself
        claim_source = SourceRef(
            url=claim.source_url,
            as_of=claim.as_of,
        )

        # Build ClaimView
        claim_view = ClaimView(
            metric=claim.metric,
            period_label=claim.period_label,
            call_date=claim.call_date,
            hedge=HedgeStrength(claim.hedge_strength),
            hedge_verbatim=claim.hedge_verbatim,
            guided_low=claim.value_low,
            guided_high=claim.value_high,
            unit=Unit(claim.value_unit),
            claim_source=claim_source,
            status=resolution.status,
            reason=resolution.reason,
            actual=resolution.actual,
            actual_unit=resolution.unit,
            period_end=resolution.period.period_end if resolution.period else None,
            inputs=tuple(inputs),
            silent=silent,
        )
        claims.append(claim_view)

    # Build and return the report
    return build_report(
        isin=isin,
        symbol=symbol,
        as_of=as_of,
        quarter_start=quarter_start,
        claims=claims,
        delivery=delivery,
    )


# --- S7b technical screens ---


def technical_signals_as_of(
    session: Session, *, isin: str, t: datetime
) -> list[TechnicalSignal]:
    """Technical signal rows for `isin` with as_of <= `t`, oldest first.

    All rows for all signal types are returned (filter by signal_type in the
    caller if needed). Uses only data visible at `t` (R2).
    """
    require_aware(t, "t")
    ts = TechnicalSignal
    return list(
        session.scalars(
            select(ts)
            .where(ts.isin == isin, ts.as_of <= t)
            .order_by(ts.signal_date, ts.id)
        )
    )


def watchlist_entries_as_of(
    session: Session, *, isin: str, t: datetime
) -> list[WatchlistEntry]:
    """Watchlist entries for `isin` with as_of <= `t`, oldest first.

    All lifecycle statuses are returned (filter by status in the caller).
    Uses only data visible at `t` (R2).
    """
    require_aware(t, "t")
    we = WatchlistEntry
    return list(
        session.scalars(
            select(we)
            .where(we.isin == isin, we.as_of <= t)
            .order_by(we.opened_date, we.id)
        )
    )


# ---------------------------------------------------------------------------
# Credit rating actions
# ---------------------------------------------------------------------------


def rating_actions_as_of(
    session: Session,
    *,
    isin: str,
    t: datetime,
) -> list[RatingAction]:
    """All rating actions for `isin` whose action_date is visible at `t`, oldest first.

    `t` is an aware datetime; rows with `as_of <= t` are included (R2).
    """
    require_aware(t, "t")
    ra = RatingAction
    return list(
        session.scalars(
            select(ra)
            .where(ra.isin == isin, ra.as_of <= t)
            .order_by(ra.action_date, ra.id)
        )
    )


def latest_rating_as_of(
    session: Session,
    *,
    isin: str,
    agency: RatingAgency,
    t: datetime,
) -> RatingAction | None:
    """The most recent rating action for (isin, agency) visible at `t`, or None.

    "Most recent" means the row with the latest `action_date`, with `id` as
    the tiebreaker (insertion order within the same day).
    """
    require_aware(t, "t")
    ra = RatingAction
    return session.scalar(
        select(ra)
        .where(ra.isin == isin, ra.agency == agency, ra.as_of <= t)
        .order_by(ra.action_date.desc(), ra.id.desc())
        .limit(1)
    )


# --- S7 company_event ---


def company_events_as_of(
    session: Session, *, isin: str, t: datetime
) -> list[CompanyEvent]:
    """Company events for isin known by t, most recent first.

    All arithmetic reads use only data with as_of <= t (R2).
    """
    require_aware(t, "t")
    e = CompanyEvent
    stmt = (
        select(e)
        .where(e.isin == isin, e.as_of <= t)
        .order_by(e.event_date.desc(), e.id.desc())
    )
    return list(session.scalars(stmt))


def company_events_by_type_as_of(
    session: Session, *, isin: str, event_type: CompanyEventType, t: datetime
) -> list[CompanyEvent]:
    """Company events of event_type for isin known by t, most recent first."""
    require_aware(t, "t")
    e = CompanyEvent
    stmt = (
        select(e)
        .where(e.isin == isin, e.event_type == event_type, e.as_of <= t)
        .order_by(e.event_date.desc(), e.id.desc())
    )
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# Session 7c: ownership events and catalyst calendar
# ---------------------------------------------------------------------------


def insider_trades_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
) -> list[InsiderTrade]:
    """All insider trade rows for `isin` visible at `as_of`.

    Returns every row with ``as_of <= as_of``, ordered by trade_date
    descending.  The caller is responsible for deduplication when the same
    physical trade was filed more than once (different `content_hash`).
    """
    require_aware(as_of, "as_of")
    m = InsiderTrade
    stmt = (
        select(m)
        .where(m.isin == isin, m.as_of <= as_of)
        .order_by(m.trade_date.desc(), m.id.desc())
    )
    return list(session.scalars(stmt))


def stake_disclosures_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
) -> list[StakeDisclosure]:
    """All stake-disclosure rows for `isin` visible at `as_of`.

    Returns every row with ``as_of <= as_of``, ordered by disclosure_date
    descending.
    """
    require_aware(as_of, "as_of")
    m = StakeDisclosure
    stmt = (
        select(m)
        .where(m.isin == isin, m.as_of <= as_of)
        .order_by(m.disclosure_date.desc(), m.id.desc())
    )
    return list(session.scalars(stmt))


def bulk_block_deals_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
) -> list[BulkBlockDeal]:
    """All bulk/block deal rows for `isin` visible at `as_of`.

    Returns every row with ``as_of <= as_of``, ordered by deal_date
    descending.
    """
    require_aware(as_of, "as_of")
    m = BulkBlockDeal
    stmt = (
        select(m)
        .where(m.isin == isin, m.as_of <= as_of)
        .order_by(m.deal_date.desc(), m.id.desc())
    )
    return list(session.scalars(stmt))


def scheduled_events_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
) -> list[ScheduledEvent]:
    """All scheduled-event rows for `isin` visible at `as_of`.

    Returns rows with ``as_of <= as_of``, ordered by event_date descending.
    Both upcoming and past events are returned; the caller filters by
    ``event_date`` if only future events are needed.
    """
    require_aware(as_of, "as_of")
    m = ScheduledEvent
    stmt = (
        select(m)
        .where(m.isin == isin, m.as_of <= as_of)
        .order_by(m.event_date.desc(), m.id.desc())
    )
    return list(session.scalars(stmt))


def scheduled_event_exists_as_of(
    session: Session,
    *,
    isin: str,
    event_type,
    event_date: date,
    as_of: datetime,
) -> bool:
    """Check if a ScheduledEvent with matching (isin, event_type, event_date) exists at `as_of`.

    Returns True if at least one row exists with the given triple, False otherwise.
    Used for idempotence checks to prevent duplicate writes on rerun.
    """
    require_aware(as_of, "as_of")
    from core.db.models import ScheduledEventType
    m = ScheduledEvent
    stmt = (
        select(m)
        .where(
            m.isin == isin,
            m.event_type == event_type,
            m.event_date == event_date,
            m.as_of <= as_of,
        )
    )
    return session.scalars(stmt).first() is not None


def index_events_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
    index_code: IndexCode | None = None,
    status: IndexEventStatus | None = None,
) -> list[IndexEvent]:
    """All index-event rows for `isin` visible at `as_of`.

    Optionally filters by ``index_code`` and/or ``status``.  Returns rows
    ordered by announced_date descending so the most-recent announcement
    appears first.
    """
    require_aware(as_of, "as_of")
    m = IndexEvent
    stmt = (
        select(m)
        .where(m.isin == isin, m.as_of <= as_of)
        .order_by(m.announced_date.desc(), m.id.desc())
    )
    if index_code is not None:
        stmt = stmt.where(m.index_code == index_code)
    if status is not None:
        stmt = stmt.where(m.status == status)
    return list(session.scalars(stmt))


def corporate_action_versions_as_of(
    session: Session,
    *,
    isins: Collection[str],
    as_of: datetime,
    action_types: Collection[CorporateActionType] | None = None,
) -> list[CorporateAction]:
    """Every version, known by `as_of`, of every action on any of `isins`, oldest first.

    Unlike `corporate_actions_as_of` this keeps superseded versions, so the
    status history can be checked. Ordered by `as_of`, then id.
    """
    require_aware(as_of, "as_of")
    ca = CorporateAction
    stmt = select(ca).where(ca.isin.in_(list(isins)), ca.as_of <= as_of).order_by(ca.as_of, ca.id)
    if action_types is not None:
        stmt = stmt.where(ca.action_type.in_(list(action_types)))
    return list(session.scalars(stmt))


# --- Drift: post-earnings drift watchlist ---


@dataclass(frozen=True)
class DriftWatchlistEntry:
    """A drift-based watchlist entry, read-only source (never stored in watchlist_entry)."""

    drift: DriftWatch
    source: str = "drift/1"


def drift_watchlist_as_of(
    session: Session,
    *,
    isin: str,
    as_of: datetime,
) -> list[DriftWatchlistEntry]:
    """Drift-based watchlist candidates for `isin` visible at `as_of`, oldest first.

    Read-only source: returns computed DriftWatch results alongside other watchlist
    entry sources (technical signals, etc.). Never stored; computed at read time.
    Returns empty list if period_end cannot be determined from latest facts.
    """
    require_aware(as_of, "as_of")

    # Get all financial filings for this ISIN to find latest period_end
    filings = financial_filings_as_of(session, isin=isin, consolidation=Consolidation.CONSOLIDATED, as_of=as_of)
    if not filings:
        return []

    # Group by period_end and take the latest (most recent quarter)
    latest_filing = filings[-1]  # Newest first by default ordering
    period_end = latest_filing.period_end

    # Compute drift for this period
    # Use the filing's as_of as evidence_url placeholder
    try:
        watch = drift_watch_as_of(
            session,
            isin=isin,
            period_end=period_end,
            as_of=as_of,
            evidence_url=latest_filing.source_url or "unknown",
        )
        return [DriftWatchlistEntry(drift=watch)]
    except Exception:
        # If computation fails (missing data, etc.), return empty list
        return []


def drift_watch_as_of(
    session: Session,
    *,
    isin: str,
    period_end: date,
    as_of: datetime,
    evidence_url: str,
) -> DriftWatch:
    """Post-earnings drift watchlist candidate for `isin` at observation time `as_of`.

    Reads financial facts and guidance resolutions at `as_of`,
    computes YoY growth and guidance surprise, then scores the
    candidate using the drift/1 formula. Missing inputs yield
    MissingInputReason entries (R3).

    Keyword-only arguments; mirrors guidance_report_as_of style.
    """
    require_aware(as_of, "as_of")

    # Compute prior-year period end (one fiscal year earlier)
    prior_period_end = date(period_end.year - 1, period_end.month, period_end.day)

    # Read financial facts at as_of
    facts = facts_as_of(session, isin=isin, consolidation=Consolidation.CONSOLIDATED, as_of=as_of)
    facts_by_line_and_period = {(f.line_item, f.period_end): f for f in facts}

    # Compute YoY sales growth
    yoy_sales_growth_pct: tuple[Decimal, date] | None = None
    revenue_current = facts_by_line_and_period.get(("total_revenue", period_end))
    revenue_prior = facts_by_line_and_period.get(("total_revenue", prior_period_end))
    if revenue_current and revenue_prior and revenue_prior.value and revenue_prior.value > 0:
        growth = ((revenue_current.value - revenue_prior.value) / revenue_prior.value) * Decimal("100")
        yoy_sales_growth_pct = (growth, revenue_current.as_of.date())

    # Compute YoY PAT growth
    yoy_pat_growth_pct: tuple[Decimal, date] | None = None
    pat_current = facts_by_line_and_period.get(("profit_after_tax", period_end))
    pat_prior = facts_by_line_and_period.get(("profit_after_tax", prior_period_end))
    if pat_current and pat_prior:
        if pat_prior.value and pat_prior.value > 0:
            # Prior year was profitable: compute YoY growth
            growth = ((pat_current.value - pat_prior.value) / pat_prior.value) * Decimal("100")
            yoy_pat_growth_pct = (growth, pat_current.as_of.date())
        elif pat_prior.value is not None and pat_prior.value < 0:
            # Prior year was a loss: growth calculation meaningless, signal via negative value
            yoy_pat_growth_pct = (Decimal("-1"), pat_current.as_of.date())

    # Compute guidance surprise from guidance resolutions
    guidance_surprise_pct: tuple[Decimal, date] | None = None
    # TODO: Implement guidance surprise from guidance_resolutions_as_of
    # For now, pass None to signal missing data with explicit MissingInputReason
    trailing_trend_surprise_pct: tuple[Decimal, date] | None = None
    # TODO: Implement trailing trend from quarterly history
    forward_pe: tuple[Decimal, date] | None = None
    diluted_share_count: tuple[Decimal, date] | None = None
    # TODO: Implement forward PE from shareholding_pattern

    # Call the pure drift_watch function
    return drift_watch(
        isin=isin,
        period_end=period_end,
        t=as_of.date(),
        yoy_sales_growth_pct=yoy_sales_growth_pct,
        yoy_pat_growth_pct=yoy_pat_growth_pct,
        trailing_trend_surprise_pct=trailing_trend_surprise_pct,
        guidance_surprise_pct=guidance_surprise_pct,
        forward_pe=forward_pe,
        diluted_share_count=diluted_share_count,
        evidence_url=evidence_url,
    )
