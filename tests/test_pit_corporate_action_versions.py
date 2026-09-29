"""core.db.pit.corporate_action_versions_as_of: every version, not just the newest."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from core.compute.corporate_action_lifecycle import ActionVersion, lifecycle_violations
from core.db.models import CorporateAction, CorporateActionStatus, CorporateActionType, RatioBasis
from core.db.pit import corporate_action_versions_as_of
from core.timezones import IST

pytestmark = pytest.mark.db

ISIN = "INE002A01018"
OTHER = "INE009A01021"


def _add(session: Session, *, key="a", isin=ISIN, status=CorporateActionStatus.ANNOUNCED, day=1,
         kind=CorporateActionType.QIP, ex=None, h="a") -> CorporateAction:  # fmt: skip
    row = CorporateAction(
        action_key=key,
        isin=isin,
        action_type=kind,
        status=status,
        ex_date=ex,
        shares_new=100,
        issue_price=Decimal("10"),
        ratio_basis=RatioBasis.EXCHANGE_FIELD,
        source_row=1,
        rule_version="t/1",
        as_of=datetime(2026, 1, day, 10, tzinfo=IST),
        content_hash=h * 64,
        source_url="https://example.com",
        extracted_by="test.adapter",
        model_version=None,
    )
    session.add(row)
    session.flush()
    return row


def test_returns_every_version_oldest_first(session: Session):
    a1 = _add(session, day=1, h="1")
    a2 = _add(session, day=3, status=CorporateActionStatus.DATES_SET, ex=date(2026, 2, 1), h="2")
    a3 = _add(session, day=2, status=CorporateActionStatus.APPROVED, h="3")
    got = corporate_action_versions_as_of(
        session, isins=[ISIN], as_of=datetime(2026, 1, 31, tzinfo=IST)
    )
    assert [r.id for r in got] == [a1.id, a3.id, a2.id]


def test_excludes_versions_after_as_of(session: Session):
    _add(session, day=1, h="1")
    _add(session, day=5, status=CorporateActionStatus.APPROVED, h="2")
    got = corporate_action_versions_as_of(
        session, isins=[ISIN], as_of=datetime(2026, 1, 4, 23, tzinfo=IST)
    )
    assert [r.status for r in got] == [CorporateActionStatus.ANNOUNCED]


def test_as_of_is_inclusive(session: Session):
    _add(session, day=4, h="1")
    got = corporate_action_versions_as_of(session, isins=[ISIN], as_of=datetime(2026, 1, 4, 10, tzinfo=IST))
    assert len(got) == 1


def test_filters_isin_and_type(session: Session):
    _add(session, key="a", h="1")
    _add(session, key="b", isin=OTHER, h="2")
    _add(session, key="c", kind=CorporateActionType.WARRANTS, h="3")
    t = datetime(2026, 2, 1, tzinfo=IST)
    assert {r.action_key for r in corporate_action_versions_as_of(session, isins=[ISIN], as_of=t)} == {"a", "c"}
    only = corporate_action_versions_as_of(
        session, isins=[ISIN], as_of=t, action_types=[CorporateActionType.WARRANTS]
    )
    assert {r.action_key for r in only} == {"c"}


def test_naive_as_of_rejected(session: Session):
    with pytest.raises(ValueError):
        corporate_action_versions_as_of(session, isins=[ISIN], as_of=datetime(2026, 1, 1))


def test_feeds_the_lifecycle_check(session: Session):
    _add(session, day=1, status=CorporateActionStatus.DATES_SET, ex=date(2026, 3, 1), h="1")
    _add(session, day=2, status=CorporateActionStatus.APPROVED, h="2")
    rows = corporate_action_versions_as_of(session, isins=[ISIN], as_of=datetime(2026, 2, 1, tzinfo=IST))
    out = lifecycle_violations(ActionVersion(r.action_key, r.status.value, r.as_of, r.id) for r in rows)
    assert [(v.previous, v.new) for v in out] == [("dates_set", "approved")]
