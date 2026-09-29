"""order_win and order_win_quarantine: schema, constraints, migration 0020 (Session 7e)."""

from datetime import datetime
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from core.compute.hashing import content_hash
from core.db.models import OrderWinFact, OrderWinQuarantine, OrderWinQuarantineReason
from core.timezones import IST
from tests.test_migrations import REPO_ROOT

ISIN = "INE002A01018"
ANNOUNCED = datetime(2026, 5, 4, 10, 30, tzinfo=IST)
KEY = content_hash(b"announcement-1")
RAW = content_hash(b"raw-listing-1")


def win(**overrides) -> OrderWinFact:
    fields = dict(
        isin=ISIN,
        announcement_key=KEY,
        announced_on=ANNOUNCED,
        order_value_inr=Decimal("25000000000"),
        value_quote="Rs. 2,500 crore",
        counterparty="Indian Railways",
        counterparty_quote="Indian Railways",
        execution_months=Decimal("24"),
        execution_end=None,
        period_quote="within 24 months",
        matched_phrase="received an order",
        missing=[],
        rule_version="order_wins/1",
        as_of=ANNOUNCED,
        content_hash=RAW,
        source_url="https://nse.example/listing",
        extracted_by="tests",
        model_version=None,
    )
    fields.update(overrides)
    return OrderWinFact(**fields)


def quarantine(**overrides) -> OrderWinQuarantine:
    fields = dict(
        isin=ISIN,
        announcement_key=KEY,
        announced_on=ANNOUNCED,
        reason=OrderWinQuarantineReason.NO_VALUE,
        quote="received an order",
        rule_version="order_wins/1",
        as_of=ANNOUNCED,
        content_hash=RAW,
        source_url="https://nse.example/listing",
        extracted_by="tests",
        model_version=None,
    )
    fields.update(overrides)
    return OrderWinQuarantine(**fields)


def test_0020_is_the_single_head():
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0020"]
    assert script.get_revision("0020").down_revision == "0019"


def test_quarantine_reasons_are_the_compute_reasons_that_are_stored():
    # NOT_AN_ORDER is the ordinary case for most announcements and is counted, not stored.
    assert {r.value for r in OrderWinQuarantineReason} == {
        "pre_award",
        "excluded",
        "ambiguous",
        "no_value",
        "multiple_values",
        "invalid_isin",
    }


@pytest.mark.db
def test_order_win_round_trips_decimal_money(session: Session):
    session.add(win())
    session.flush()
    row = session.query(OrderWinFact).one()
    assert row.order_value_inr == Decimal("25000000000")
    assert isinstance(row.order_value_inr, Decimal)
    assert row.missing == []


@pytest.mark.db
def test_missing_list_round_trips(session: Session):
    session.add(
        win(
            counterparty=None,
            counterparty_quote=None,
            execution_months=None,
            period_quote=None,
            missing=["counterparty", "execution_period"],
        )
    )
    session.flush()
    assert session.query(OrderWinFact).one().missing == ["counterparty", "execution_period"]


@pytest.mark.db
@pytest.mark.parametrize(
    "overrides",
    [
        dict(isin="NOTANISIN"),
        dict(order_value_inr=Decimal("0")),
        dict(order_value_inr=Decimal("-5")),
        dict(counterparty_quote=None),  # a counterparty needs its quote
        dict(counterparty=None, counterparty_quote=None, missing=[]),  # a gap must be named
        dict(missing=["counterparty"]),  # a named gap must be a gap
        dict(period_quote=None),  # months need their quote
        dict(execution_months=None, period_quote=None, missing=[]),  # a gap must be named
        dict(model_version="claude-x"),  # no model wrote this
        dict(content_hash="XYZ"),
    ],
)
def test_order_win_constraints_reject(session: Session, overrides):
    session.add(win(**overrides))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
def test_same_announcement_and_rule_version_is_unique(session: Session):
    session.add(win())
    session.flush()
    session.add(win(as_of=datetime(2026, 5, 4, 11, 0, tzinfo=IST)))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
def test_a_new_rule_version_may_add_a_version_of_the_same_announcement(session: Session):
    session.add(win())
    session.add(win(rule_version="order_wins/2"))
    session.flush()
    assert session.query(OrderWinFact).count() == 2


@pytest.mark.db
def test_order_win_is_append_only(session: Session):
    session.add(win())
    session.flush()
    with pytest.raises(DBAPIError):
        session.execute(text("UPDATE order_win SET value_quote = 'x'"))


@pytest.mark.db
def test_quarantine_round_trip_and_uniqueness(session: Session):
    session.add(quarantine())
    session.flush()
    assert session.query(OrderWinQuarantine).one().reason is OrderWinQuarantineReason.NO_VALUE
    session.add(quarantine())
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
def test_quarantine_isin_is_null_or_well_formed(session: Session):
    session.add(quarantine(isin=None, reason=OrderWinQuarantineReason.INVALID_ISIN, quote="BAD"))
    session.flush()
    session.add(quarantine(isin="BAD", announcement_key=content_hash(b"other")))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
def test_quarantine_is_append_only(session: Session):
    session.add(quarantine())
    session.flush()
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM order_win_quarantine"))
