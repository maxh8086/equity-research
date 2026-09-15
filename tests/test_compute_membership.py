from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.membership import (
    ConflictingSnapshots,
    Membership,
    ObservedInterval,
    Snapshot,
    membership_on,
    observed_intervals,
)

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def at(days: int) -> datetime:
    return T0 + timedelta(days=days)


def snap(days: int, *isins: str, complete: bool = True) -> Snapshot:
    return Snapshot(at(days), frozenset(isins), complete)


def members(*isins: str, uncertain: str = "") -> Membership:
    return Membership(frozenset(isins), frozenset(uncertain))


def test_runs_are_membership_and_the_edges_between_snapshots_are_uncertain():
    intervals = observed_intervals([snap(0, "A", "B"), snap(100, "A", "B"), snap(200, "A", "C")])
    assert intervals == [
        ObservedInterval("A", at(0), at(200), None, None),
        ObservedInterval("B", at(0), at(100), None, at(200)),
        ObservedInterval("C", at(200), at(200), at(100), None),
    ]
    assert membership_on(intervals, at(50)) == members("A", "B")
    assert membership_on(intervals, at(150)) == members("A", uncertain="BC")
    assert membership_on(intervals, at(200)) == members("A", "C")


def test_before_the_first_snapshot_nobody_is_a_member():
    assert membership_on(observed_intervals([snap(10, "A")]), at(0)) == members(uncertain="A")


def test_after_the_last_snapshot_open_intervals_are_uncertain():
    assert membership_on(observed_intervals([snap(0, "A")]), at(1)) == members(uncertain="A")


def test_absence_from_an_incomplete_snapshot_does_not_end_a_run():
    intervals = observed_intervals([snap(0, "A", "B"), snap(100, "A", complete=False), snap(200, "A", "B")])
    assert [(i.isin, i.first_seen, i.last_seen) for i in intervals] == [
        ("A", at(0), at(200)),
        ("B", at(0), at(200)),
    ]
    assert membership_on(intervals, at(100)) == members("A", "B")


def test_leaving_and_rejoining_makes_two_intervals():
    intervals = observed_intervals([snap(0, "A"), snap(100), snap(200, "A")])
    assert [(i.first_seen, i.last_seen) for i in intervals] == [(at(0), at(0)), (at(200), at(200))]
    assert membership_on(intervals, at(100)) == members()


@pytest.mark.parametrize(
    "second",
    [snap(0, "A", "B"), Snapshot(at(0).astimezone(timezone(timedelta(hours=5, minutes=30))), frozenset("B"), True)],
    ids=["different-list", "same-instant-other-zone"],
)
def test_complete_snapshots_at_one_instant_must_agree(second):
    with pytest.raises(ConflictingSnapshots):
        observed_intervals([snap(0, "A"), second])


def test_an_incomplete_snapshot_may_list_fewer_at_the_same_instant():
    observed_intervals([snap(0, "A", "B"), snap(0, "A", complete=False)])


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #

snapshot_lists = st.lists(
    st.tuples(st.integers(0, 3650), st.frozensets(st.sampled_from("ABCDEF")), st.booleans()),
    max_size=12,
    unique_by=lambda row: row[0],
).map(lambda rows: [Snapshot(at(d), isins, complete) for d, isins, complete in rows])


@given(snapshot_lists)
def test_a_snapshot_reads_back_at_its_own_instant(snapshots):
    intervals = observed_intervals(snapshots)
    for s in snapshots:
        read = membership_on(intervals, s.observed_at)
        if s.complete:
            assert read == Membership(s.isins, frozenset())
        else:
            assert s.isins <= read.members


@given(snapshot_lists, st.integers(-10, 3700))
def test_members_and_uncertain_are_disjoint_and_come_from_snapshots(snapshots, day):
    read = membership_on(observed_intervals(snapshots), at(day))
    assert not read.members & read.uncertain
    assert read.members | read.uncertain <= frozenset().union(*(s.isins for s in snapshots))


@given(snapshot_lists.flatmap(lambda s: st.tuples(st.just(s), st.permutations(s))))
def test_input_order_does_not_matter(pair):
    snapshots, shuffled = pair
    assert observed_intervals(snapshots) == observed_intervals(shuffled)
