from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.db.base import Base
from core.db.models import FactKind, FinancialFact
from core.timezones import IST
from tests.factories import make_fact

pytestmark = pytest.mark.db


def _assert_rejected(session, fact, exc=IntegrityError):
    with pytest.raises(exc):
        with session.begin_nested():
            session.add(fact)
            session.flush()


def test_models_match_migrations(engine):
    """Every schema change is a migration: ORM metadata must equal the migrated DB."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_valid_fact_round_trips_decimal_exactly(session):
    session.add(make_fact(value=Decimal("1234567890123456789012.123456")))
    session.flush()
    stored = session.scalars(select(FinancialFact)).one()
    assert stored.value == Decimal("1234567890123456789012.123456")
    assert isinstance(stored.value, Decimal)
    assert stored.as_of.utcoffset() is not None
    assert stored.ingested_at is not None


def test_naive_as_of_rejected_before_reaching_db():
    with pytest.raises(ValueError, match="timezone-aware"):
        make_fact(as_of=datetime(2024, 4, 22, 16, 30))


def test_as_of_on_period_end_day_in_ist_rejected(session):
    _assert_rejected(session, make_fact(as_of=datetime(2024, 3, 31, 23, 0, tzinfo=IST)))


def test_as_of_boundary_is_evaluated_in_ist_not_utc(session):
    # 20:00 UTC on 31 Mar is 01:30 IST on 1 Apr: the quarter has closed in India.
    session.add(make_fact(as_of=datetime(2024, 3, 31, 20, 0, tzinfo=timezone.utc)))
    session.flush()
    # 18:00 UTC on 31 Mar is 23:30 IST on 31 Mar: still inside the period.
    _assert_rejected(
        session,
        make_fact(
            line_item="other_income",
            as_of=datetime(2024, 3, 31, 18, 0, tzinfo=timezone.utc),
        ),
    )


@pytest.mark.parametrize("isin", ["INE002A0101", "US0378331005", "ine002a01018"])
def test_non_indian_or_malformed_isin_rejected(session, isin):
    _assert_rejected(session, make_fact(isin=isin))


def test_period_start_after_end_rejected(session):
    _assert_rejected(session, make_fact(period_start=date(2024, 4, 1)))


def test_reported_fact_requires_xbrl_element(session):
    _assert_rejected(session, make_fact(xbrl_element=None))


def test_computed_fact_must_not_claim_xbrl_element(session):
    _assert_rejected(session, make_fact(fact_kind=FactKind.COMPUTED, line_item="roce"))


def test_model_version_forbidden(session):
    _assert_rejected(session, make_fact(model_version="claude-sonnet-5"))


def test_bad_content_hash_rejected(session):
    _assert_rejected(session, make_fact(content_hash="not-a-sha256"))


def test_duplicate_version_rejected_including_null_period_start(session):
    session.add(make_fact(period_start=None, line_item="total_assets"))
    session.flush()
    _assert_rejected(session, make_fact(period_start=None, line_item="total_assets"))


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE financial_facts SET value = 0",
        "DELETE FROM financial_facts",
        "TRUNCATE financial_facts",
    ],
)
def test_store_is_append_only(session, sql):
    session.add(make_fact())
    session.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text(sql))
