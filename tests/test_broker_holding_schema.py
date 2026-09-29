"""broker_holding_snapshot and broker_holding_quarantine: schema, constraints, migration 0021."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from core.compute.hashing import content_hash
from core.db.models import (
    BrokerHoldingQuarantine,
    BrokerHoldingQuarantineReason,
    BrokerHoldingSnapshot,
)
from core.timezones import IST
from tests.test_migrations import REPO_ROOT

ISIN = "INE000A01004"
SNAP = datetime(2026, 9, 29, 16, 0, tzinfo=IST)
RAW = content_hash(b"synthetic-drop-1")


def holding(**overrides) -> BrokerHoldingSnapshot:
    fields = dict(
        account_label="family-main",
        isin=ISIN,
        exchange="NSE",
        quantity=100,
        average_price=Decimal("250.5"),
        last_price=Decimal("300.25"),
        snapshot_at=SNAP,
        as_of=SNAP,
        content_hash=RAW,
        source_url="manual://broker-holdings/synthetic",
        extracted_by="tests",
        model_version=None,
    )
    fields.update(overrides)
    return BrokerHoldingSnapshot(**fields)


def quarantine(**overrides) -> BrokerHoldingQuarantine:
    fields = dict(
        account_label="family-main",
        isin=None,
        row_number=3,
        reason=BrokerHoldingQuarantineReason.MISSING_ISIN,
        detail="isin absent",
        snapshot_at=SNAP,
        as_of=SNAP,
        content_hash=RAW,
        source_url="manual://broker-holdings/synthetic",
        extracted_by="tests",
        model_version=None,
    )
    fields.update(overrides)
    return BrokerHoldingQuarantine(**fields)


def test_0021_follows_0020_and_is_the_single_head():
    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert script.get_heads() == ["0021"]
    assert script.get_revision("0021").down_revision == "0020"


def test_quarantine_reasons():
    assert {r.value for r in BrokerHoldingQuarantineReason} == {
        "missing_isin",
        "invalid_isin",
        "negative_quantity",
        "unparseable_number",
        "unknown_exchange",
        "duplicate_row",
    }


@pytest.mark.db
def test_snapshot_round_trips_decimal_money_and_integer_quantity(session: Session):
    session.add(holding(average_price=Decimal("1234.55")))
    session.flush()
    row = session.query(BrokerHoldingSnapshot).one()
    assert row.average_price == Decimal("1234.55")
    assert isinstance(row.average_price, Decimal)
    assert isinstance(row.quantity, int)
    assert row.snapshot_at == SNAP


@pytest.mark.db
@pytest.mark.parametrize(
    "overrides",
    [
        dict(isin="NOTANISIN"),
        dict(exchange="NYSE"),
        dict(quantity=-1),
        dict(average_price=Decimal("-0.01")),
        dict(last_price=Decimal("-0.01")),
        dict(account_label=""),
        dict(as_of=SNAP - timedelta(seconds=1)),  # known before it was taken
        dict(model_version="m"),
        dict(content_hash="xyz"),
    ],
)
def test_snapshot_constraints(session: Session, overrides):
    session.add(holding(**overrides))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
def test_zero_quantity_is_allowed(session: Session):
    session.add(holding(quantity=0))
    session.flush()


@pytest.mark.db
def test_same_key_twice_is_refused_but_other_exchange_or_file_is_not(session: Session):
    session.add(holding())
    session.add(holding(exchange="BSE"))
    session.add(holding(content_hash=content_hash(b"corrected"), as_of=SNAP + timedelta(hours=1)))
    session.add(holding(account_label="other"))
    session.flush()
    session.add(holding())
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.db
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE broker_holding_snapshot SET quantity = 1",
        "DELETE FROM broker_holding_snapshot",
        "TRUNCATE broker_holding_snapshot",
    ],
)
def test_snapshot_is_append_only(session: Session, statement):
    session.add(holding())
    session.flush()
    with pytest.raises(DBAPIError):
        session.execute(text(statement))


@pytest.mark.db
def test_quarantine_round_trips_with_null_isin_and_is_append_only(session: Session):
    session.add(quarantine())
    session.add(
        quarantine(
            row_number=4, isin="INE000A0100X", reason=BrokerHoldingQuarantineReason.INVALID_ISIN
        )
    )
    session.flush()
    rows = session.query(BrokerHoldingQuarantine).order_by(BrokerHoldingQuarantine.row_number).all()
    assert [r.isin for r in rows] == [None, "INE000A0100X"]
    with pytest.raises(DBAPIError):
        session.execute(text("DELETE FROM broker_holding_quarantine"))


@pytest.mark.db
def test_quarantine_one_reason_per_row_per_file(session: Session):
    session.add(quarantine())
    session.flush()
    session.add(quarantine(reason=BrokerHoldingQuarantineReason.NEGATIVE_QUANTITY))
    with pytest.raises(IntegrityError):
        session.flush()
