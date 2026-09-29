"""order_wins_as_of: order-win versions known at `as_of`, through pit.py (Session 7e)."""

from datetime import datetime, timedelta

import pytest

from core.compute.hashing import content_hash
from core.db.pit import order_wins_as_of
from core.timezones import IST
from tests.test_order_win_schema import ANNOUNCED, ISIN, win

pytestmark = pytest.mark.db

OTHER_ISIN = "INE467B01029"
K1 = content_hash(b"k1")
K2 = content_hash(b"k2")
K3 = content_hash(b"k3")
AFTER = ANNOUNCED + timedelta(days=1)


def test_only_rows_public_at_as_of(session):
    session.add(win(announcement_key=K1))
    session.add(win(announcement_key=K2, announced_on=AFTER, as_of=AFTER))
    session.flush()
    rows = order_wins_as_of(session, isin=ISIN, as_of=ANNOUNCED)
    assert [r.announcement_key for r in rows] == [K1]
    rows = order_wins_as_of(session, isin=ISIN, as_of=ANNOUNCED - timedelta(seconds=1))
    assert rows == []


def test_other_isins_are_not_returned(session):
    session.add(win(announcement_key=K1))
    session.add(win(announcement_key=K2, isin=OTHER_ISIN))
    session.flush()
    assert [r.isin for r in order_wins_as_of(session, isin=OTHER_ISIN, as_of=AFTER)] == [OTHER_ISIN]


def test_newest_announcement_first(session):
    session.add(win(announcement_key=K1))
    session.add(win(announcement_key=K2, announced_on=AFTER, as_of=AFTER))
    session.flush()
    rows = order_wins_as_of(session, isin=ISIN, as_of=AFTER)
    assert [r.announcement_key for r in rows] == [K2, K1]


def test_latest_version_of_an_announcement_known_at_as_of(session):
    later = ANNOUNCED + timedelta(hours=6)
    session.add(win(announcement_key=K1, rule_version="order_wins/1"))
    session.add(win(announcement_key=K1, rule_version="order_wins/2", as_of=later, counterparty="NHAI",
                    counterparty_quote="NHAI"))
    session.flush()
    early = order_wins_as_of(session, isin=ISIN, as_of=ANNOUNCED)
    assert [(r.rule_version, r.counterparty) for r in early] == [("order_wins/1", "Indian Railways")]
    late = order_wins_as_of(session, isin=ISIN, as_of=later)
    assert [(r.rule_version, r.counterparty) for r in late] == [("order_wins/2", "NHAI")]


def test_same_as_of_versions_resolve_by_insertion_order(session):
    session.add(win(announcement_key=K1, rule_version="order_wins/1"))
    session.add(win(announcement_key=K1, rule_version="order_wins/2"))
    session.flush()
    rows = order_wins_as_of(session, isin=ISIN, as_of=AFTER)
    assert [r.rule_version for r in rows] == ["order_wins/2"]


def test_naive_as_of_is_refused(session):
    with pytest.raises(ValueError):
        order_wins_as_of(session, isin=ISIN, as_of=datetime(2026, 5, 5))
