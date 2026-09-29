"""Migration 0019: fund-raising action types, promoter-allottee flag, demerger milestone events.

The `engine` fixture already runs upgrade, downgrade to base and upgrade again
against the Docker database, so a broken up/down fails every db test.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db.models import (
    CorporateAction,
    CorporateActionStatus,
    CorporateActionType,
    RatioBasis,
    ScheduledEventType,
)
from core.timezones import IST
from tests.test_migrations import REPO_ROOT

FUNDRAISING = ("qip", "preferential_allotment", "warrants", "esop", "fccb")
DEMERGER_EVENTS = (
    "demerger_scheme",
    "demerger_board",
    "demerger_shareholder",
    "demerger_creditor",
    "demerger_nclt_order",
    "demerger_listing",
)


def test_0019_follows_0018_in_a_single_chain():
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0019").down_revision == "0018"


def test_python_enums_carry_new_values():
    assert {t.value for t in CorporateActionType} >= set(FUNDRAISING)
    assert {t.value for t in ScheduledEventType} >= set(DEMERGER_EVENTS)


@pytest.mark.db
def test_database_enums_carry_new_values(session: Session):
    def labels(name: str) -> set[str]:
        rows = session.execute(
            text("SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :n"),
            {"n": name},
        )
        return {r[0] for r in rows}

    assert labels("corporate_action_type") >= set(FUNDRAISING)
    assert labels("scheduled_event_type") >= set(DEMERGER_EVENTS)


def _row(**kw) -> CorporateAction:
    base = dict(
        action_key="k1",
        isin="INE002A01018",
        action_type=CorporateActionType.QIP,
        status=CorporateActionStatus.DATES_SET,
        ex_date=date(2026, 3, 10),
        shares_new=1000,
        issue_price=Decimal("2500.0000"),
        ratio_basis=RatioBasis.EXCHANGE_FIELD,
        source_row=1,
        rule_version="t/1",
        as_of=datetime(2026, 3, 1, 10, tzinfo=IST),
        content_hash="a" * 64,
        source_url="https://example.com/x",
        extracted_by="test.adapter",
        model_version=None,
    )
    base.update(kw)
    return CorporateAction(**base)


@pytest.mark.db
def test_fundraising_row_with_promoter_flag_is_stored(session: Session):
    row = _row(action_type=CorporateActionType.PREFERENTIAL_ALLOTMENT, allottee_is_promoter=True)
    session.add(row)
    session.flush()
    assert row.allottee_is_promoter is True


@pytest.mark.db
def test_promoter_flag_defaults_to_null(session: Session):
    row = _row()
    session.add(row)
    session.flush()
    assert row.allottee_is_promoter is None


@pytest.mark.db
@pytest.mark.parametrize("kind", ["qip", "preferential_allotment", "warrants", "esop", "fccb"])
def test_fundraising_needs_new_shares_once_dates_set(session: Session, kind: str):
    session.add(_row(action_type=CorporateActionType(kind), shares_new=None))
    with pytest.raises(IntegrityError, match="ck_corporate_action_terms"):
        session.flush()


@pytest.mark.db
@pytest.mark.parametrize("kind", ["qip", "preferential_allotment", "warrants"])
def test_priced_fundraising_needs_issue_price(session: Session, kind: str):
    session.add(_row(action_type=CorporateActionType(kind), issue_price=None))
    with pytest.raises(IntegrityError, match="ck_corporate_action_terms"):
        session.flush()


@pytest.mark.db
def test_announced_fundraising_may_lack_terms(session: Session):
    session.add(
        _row(status=CorporateActionStatus.ANNOUNCED, ex_date=None, shares_new=None, issue_price=None)
    )
    session.flush()


@pytest.mark.db
def test_existing_terms_still_enforced(session: Session):
    session.add(_row(action_type=CorporateActionType.BONUS, shares_new=0, shares_held=1, issue_price=None))
    with pytest.raises(IntegrityError, match="ck_corporate_action_terms"):
        session.flush()
