from datetime import date, datetime
from decimal import Decimal

import pytest

from core.db.models import Consolidation
from core.db.pit import facts_as_of
from core.timezones import IST
from tests.factories import RELIANCE, make_fact

pytestmark = pytest.mark.db

ORIGINAL_AT = datetime(2024, 4, 22, 16, 30, tzinfo=IST)
RESTATED_AT = datetime(2024, 7, 19, 16, 30, tzinfo=IST)


def _read(session, as_of, **kw):
    return facts_as_of(
        session, isin=RELIANCE, consolidation=Consolidation.CONSOLIDATED, as_of=as_of, **kw
    )


@pytest.fixture
def restated(session):
    session.add_all(
        [
            make_fact(value=Decimal("100"), as_of=ORIGINAL_AT),
            make_fact(value=Decimal("90"), as_of=RESTATED_AT),
        ]
    )
    session.flush()


def test_before_original_filing_nothing_is_known(session, restated):
    assert _read(session, datetime(2024, 4, 22, 16, 29, tzinfo=IST)) == []


def test_read_between_filings_sees_original_not_restatement(session, restated):
    [fact] = _read(session, datetime(2024, 6, 1, tzinfo=IST))
    assert fact.value == Decimal("100")


def test_as_of_boundary_is_inclusive(session, restated):
    [fact] = _read(session, RESTATED_AT)
    assert fact.value == Decimal("90")


def test_read_after_restatement_sees_latest_version_only(session, restated):
    [fact] = _read(session, datetime(2025, 1, 1, tzinfo=IST))
    assert fact.value == Decimal("90")


def test_instant_and_duration_facts_are_distinct_series(session):
    session.add_all(
        [
            make_fact(line_item="total_assets", period_start=None, value=Decimal("1")),
            make_fact(line_item="total_assets", period_start=None, value=Decimal("2"),
                      as_of=RESTATED_AT),
            make_fact(line_item="total_assets", period_start=date(2023, 4, 1),
                      value=Decimal("3")),
        ]
    )
    session.flush()
    facts = _read(session, datetime(2025, 1, 1, tzinfo=IST), line_items=["total_assets"])
    assert sorted(f.value for f in facts) == [Decimal("2"), Decimal("3")]


def test_other_consolidation_and_isin_not_leaked(session):
    session.add_all(
        [
            make_fact(consolidation=Consolidation.STANDALONE),
            make_fact(isin="INE467B01029"),
        ]
    )
    session.flush()
    assert _read(session, datetime(2025, 1, 1, tzinfo=IST)) == []


def test_naive_as_of_raises(session):
    with pytest.raises(ValueError, match="timezone-aware"):
        _read(session, datetime(2025, 1, 1))
