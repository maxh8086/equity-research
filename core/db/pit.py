"""Point-in-time reads. Every read takes an explicit `as_of`; there is no default."""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.compute.adjustment import (
    PriceFactor,
    adjust_series,
    bonus_factor,
    demerger_factor,
    rights_factor,
    split_factor,
)
from core.compute.membership import (
    Membership,
    ObservedInterval,
    Snapshot,
    membership_on,
    observed_intervals,
)
from core.compute.price_crosscheck import Bar, Mismatch, crosscheck
from core.db.models import (
    Consolidation,
    CorporateAction,
    CorporateActionQuarantine,
    CorporateActionStatus,
    CorporateActionType,
    EntityIsin,
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFiling,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    IndexSnapshotQuarantine,
    NseBhavcopyQuarantine,
    NseBhavcopyRow,
    RatioBasis,
    RawSourceFile,
    UpstoxCandle,
    UpstoxCandleQuarantine,
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
