"""holdings_as_of: the latest snapshot per ISIN known at `as_of`, through pit.py."""

from datetime import datetime, timedelta

import pytest

from core.compute.hashing import content_hash
from core.db.pit import broker_holdings_file_loaded, holdings_as_of
from core.timezones import IST
from tests.test_broker_holding_schema import ISIN, RAW, SNAP, holding

pytestmark = pytest.mark.db

OTHER = "INE000B01002"
LATER = SNAP + timedelta(days=1)
RAW2 = content_hash(b"synthetic-drop-2")


def test_only_snapshots_known_at_as_of(session):
    session.add(holding())
    session.add(holding(snapshot_at=LATER, as_of=LATER, content_hash=RAW2, quantity=200))
    session.flush()
    rows = holdings_as_of(session, account_label="family-main", as_of=SNAP)
    assert [r.quantity for r in rows] == [100]
    assert holdings_as_of(session, account_label="family-main", as_of=SNAP - timedelta(seconds=1)) == []


def test_as_of_is_when_known_not_when_taken(session):
    # Taken at SNAP but only dropped a day later: invisible before then.
    session.add(holding(as_of=LATER))
    session.flush()
    assert holdings_as_of(session, account_label="family-main", as_of=SNAP) == []
    assert len(holdings_as_of(session, account_label="family-main", as_of=LATER)) == 1


def test_latest_snapshot_wins_per_isin(session):
    session.add(holding())
    session.add(holding(snapshot_at=LATER, as_of=LATER, content_hash=RAW2, quantity=200))
    session.flush()
    rows = holdings_as_of(session, account_label="family-main", as_of=LATER)
    assert [(r.isin, r.quantity) for r in rows] == [(ISIN, 200)]


def test_each_isin_keeps_its_own_latest_snapshot(session):
    # OTHER is absent from the later snapshot: its last known row stays. A holding
    # sold in between is not inferred from absence (documented limit).
    session.add(holding())
    session.add(holding(isin=OTHER, quantity=7))
    session.add(holding(snapshot_at=LATER, as_of=LATER, content_hash=RAW2, quantity=200))
    session.flush()
    rows = holdings_as_of(session, account_label="family-main", as_of=LATER)
    assert [(r.isin, r.quantity) for r in rows] == [(ISIN, 200), (OTHER, 7)]


def test_other_accounts_are_not_returned(session):
    session.add(holding())
    session.add(holding(account_label="someone-else", quantity=5))
    session.flush()
    rows = holdings_as_of(session, account_label="someone-else", as_of=LATER)
    assert [r.quantity for r in rows] == [5]


def test_both_exchanges_of_one_isin_in_the_same_snapshot_are_returned(session):
    session.add(holding(exchange="NSE", quantity=100))
    session.add(holding(exchange="BSE", quantity=20))
    session.flush()
    rows = holdings_as_of(session, account_label="family-main", as_of=LATER)
    assert sorted((r.exchange, r.quantity) for r in rows) == [("BSE", 20), ("NSE", 100)]


def test_correction_of_the_same_snapshot_replaces_it_once_known(session):
    fixed = SNAP + timedelta(hours=2)
    session.add(holding(quantity=100))
    session.add(holding(quantity=90, as_of=fixed, content_hash=RAW2))
    session.flush()
    before = holdings_as_of(session, account_label="family-main", as_of=SNAP + timedelta(hours=1))
    after = holdings_as_of(session, account_label="family-main", as_of=fixed)
    assert [r.quantity for r in before] == [100]
    assert [r.quantity for r in after] == [90]


def test_naive_as_of_is_refused(session):
    with pytest.raises(ValueError):
        holdings_as_of(session, account_label="family-main", as_of=datetime(2026, 9, 30))


def test_file_loaded_check(session):
    assert not broker_holdings_file_loaded(session, content_hash=RAW)
    session.add(holding())
    session.flush()
    assert broker_holdings_file_loaded(session, content_hash=RAW)
    assert not broker_holdings_file_loaded(session, content_hash=RAW2)


def test_ist_aware_as_of_is_accepted(session):
    assert holdings_as_of(session, account_label="x", as_of=datetime(2026, 9, 30, tzinfo=IST)) == []
