"""Point-in-time reads. Every read takes an explicit `as_of`; there is no default."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.compute.membership import (
    Membership,
    ObservedInterval,
    Snapshot,
    membership_on,
    observed_intervals,
)
from core.db.models import (
    Consolidation,
    EntityIsin,
    FinancialFact,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    IndexSnapshotQuarantine,
    RawSourceFile,
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
