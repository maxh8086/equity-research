"""Index membership from dated constituent snapshots. Pure functions, no I/O.

A snapshot is one published constituent list: the ISINs in an index at
`observed_at`. Snapshots are sparse (archive captures can be months apart), so
membership between them is inferred only as far as is safe:

- between snapshots that all list an ISIN, it is taken as a member (a run);
  an exit and re-entry inside such a gap would be missed
- between the last snapshot listing it and the next complete snapshot without
  it, it is *uncertain*, never guessed either way
- a snapshot with quarantined rows is incomplete: it confirms who is present
  but cannot show that anyone is absent

Knowledge time is the caller's job: pass only snapshots with `as_of <= t`
(core.db.pit does).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Snapshot:
    observed_at: datetime
    isins: frozenset[str]
    complete: bool


@dataclass(frozen=True)
class ObservedInterval:
    """One run of snapshots listing `isin`.

    A member on [first_seen, last_seen]. Uncertain on (absent_before, first_seen)
    and (last_seen, absent_after), where None means no complete snapshot bounds
    that side.
    """

    isin: str
    first_seen: datetime
    last_seen: datetime
    absent_before: datetime | None
    absent_after: datetime | None


@dataclass(frozen=True)
class Membership:
    members: frozenset[str]
    uncertain: frozenset[str]


class ConflictingSnapshots(ValueError):
    """Two lists published at the same instant disagree."""


def _check_simultaneous(snapshots: Iterable[Snapshot]) -> None:
    by_instant: dict[datetime, list[Snapshot]] = defaultdict(list)
    for s in snapshots:
        by_instant[s.observed_at].append(s)
    for instant, group in by_instant.items():
        listed = frozenset().union(*(s.isins for s in group))
        for s in group:
            if s.complete and s.isins != listed:
                raise ConflictingSnapshots(
                    f"snapshots at {instant} disagree on {sorted(listed - s.isins)}"
                )


def observed_intervals(snapshots: Iterable[Snapshot]) -> list[ObservedInterval]:
    ordered = sorted(snapshots, key=lambda s: s.observed_at)
    _check_simultaneous(ordered)
    intervals = []
    for isin in sorted(frozenset().union(*(s.isins for s in ordered))):
        run_start = run_end = run_before = last_absent = None
        for s in ordered:
            if isin in s.isins:
                if run_start is None:
                    run_start, run_before = s.observed_at, last_absent
                run_end = s.observed_at
            elif s.complete:
                if run_start is not None:
                    intervals.append(
                        ObservedInterval(isin, run_start, run_end, run_before, s.observed_at)
                    )
                    run_start = None
                last_absent = s.observed_at
        if run_start is not None:
            intervals.append(ObservedInterval(isin, run_start, run_end, run_before, None))
    return intervals


def membership_on(intervals: Iterable[ObservedInterval], at: datetime) -> Membership:
    """Who was in the index at `at`, and whose membership the snapshots cannot settle."""
    members: set[str] = set()
    uncertain: set[str] = set()
    for i in intervals:
        if i.first_seen <= at <= i.last_seen:
            members.add(i.isin)
        elif at < i.first_seen and (i.absent_before is None or i.absent_before < at):
            uncertain.add(i.isin)
        elif i.last_seen < at and (i.absent_after is None or at < i.absent_after):
            uncertain.add(i.isin)
    return Membership(frozenset(members), frozenset(uncertain - members))
