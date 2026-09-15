"""Point-in-time reads. Every read takes an explicit `as_of`; there is no default."""

from collections.abc import Collection, Sequence
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
    """Constituent lists for `index_code` published by `as_of`, oldest first."""
    require_aware(as_of, "as_of")
    s, c = IndexSnapshot, IndexSnapshotConstituent
    snapshots = list(
        session.scalars(
            select(s).where(s.index_code == index_code, s.as_of <= as_of).order_by(s.as_of, s.id)
        )
    )
    isins: dict[int, set[str]] = {snap.id: set() for snap in snapshots}
    rows = session.execute(
        select(c.snapshot_id, c.isin)
        .join(s, s.id == c.snapshot_id)
        .where(s.index_code == index_code, s.as_of <= as_of, c.as_of <= as_of)
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
    session: Session, *, source_url: str, as_of: datetime
) -> set[tuple[IndexCode, datetime]]:
    """(index, publication time) of snapshots recorded from `source_url` by `as_of`."""
    require_aware(as_of, "as_of")
    s = IndexSnapshot
    rows = session.execute(
        select(s.index_code, s.as_of).where(s.source_url == source_url, s.as_of <= as_of)
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
