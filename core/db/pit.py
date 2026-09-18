"""Point-in-time reads. Every read takes an explicit `as_of`; there is no default."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    EntityIsin,
    FinancialFact,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    IndexSnapshotQuarantine,
    NseBhavcopyQuarantine,
    NseBhavcopyRow,
    RawSourceFile,
    UpstoxCandle,
    UpstoxCandleQuarantine,
)
from core.timezones import require_aware


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


def symbol_to_isin_as_of(session: Session, *, symbol: str, on: date, as_of: datetime) -> str | None:
    """The ISIN bhavcopy showed for `symbol` on the latest trade_date <= `on`, known by `as_of`.

    Symbols are reused across companies over decades (CLAUDE.md "Universe"),
    so this is a snapshot at a date, never a standing lookup table. None if
    `symbol` never traded on or before `on` in what is known by `as_of`.
    """
    require_aware(as_of, "as_of")
    r = NseBhavcopyRow
    rows = list(
        session.scalars(
            select(r).where(r.symbol == symbol, r.trade_date <= on, r.as_of <= as_of).order_by(r.id)
        )
    )
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
