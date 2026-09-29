"""trailing_revenue_as_of: quarterly revenue facts known at `as_of`, through pit.py."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from core.db.models import Consolidation
from core.db.pit import trailing_revenue_as_of
from core.timezones import IST
from tests.factories import RELIANCE, make_fact

pytestmark = pytest.mark.db

D = Decimal
CR = D("10000000")
CONS = Consolidation.CONSOLIDATED
QUARTERS = [
    (date(2025, 1, 1), date(2025, 3, 31), 1000, datetime(2025, 4, 25, tzinfo=IST)),
    (date(2025, 4, 1), date(2025, 6, 30), 1100, datetime(2025, 7, 25, tzinfo=IST)),
    (date(2025, 7, 1), date(2025, 9, 30), 1200, datetime(2025, 10, 25, tzinfo=IST)),
    (date(2025, 10, 1), date(2025, 12, 31), 1300, datetime(2026, 1, 25, tzinfo=IST)),
]
NOW = datetime(2026, 5, 5, tzinfo=IST)


def _load(session):
    for start, end, cr, as_of in QUARTERS:
        session.add(make_fact(period_start=start, period_end=end, value=D(cr) * CR, as_of=as_of))
    session.flush()


def test_sums_four_quarters_known_at_as_of(session):
    _load(session)
    tr = trailing_revenue_as_of(session, isin=RELIANCE, consolidation=CONS, as_of=NOW)
    assert tr.revenue_inr == D("4600") * CR
    assert tr.period_end == date(2025, 12, 31)
    assert tr.as_of == datetime(2026, 1, 25, tzinfo=IST)


def test_later_filed_quarter_is_invisible_earlier(session):
    _load(session)
    tr = trailing_revenue_as_of(
        session, isin=RELIANCE, consolidation=CONS, as_of=datetime(2026, 1, 1, tzinfo=IST)
    )
    assert tr is None  # only three quarters were public


def test_none_when_no_facts(session):
    assert trailing_revenue_as_of(session, isin=RELIANCE, consolidation=CONS, as_of=NOW) is None


def test_ignores_annual_and_other_line_items(session):
    _load(session)
    session.add(make_fact(
        period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
        value=D("99999") * CR, as_of=datetime(2026, 1, 26, tzinfo=IST),
    ))
    session.add(make_fact(
        line_item="profit_for_period", xbrl_element="in-bse-fin:ProfitLossForPeriod",
        period_start=date(2025, 10, 1), period_end=date(2025, 12, 31),
        value=D("1") * CR, as_of=datetime(2026, 1, 25, tzinfo=IST),
    ))
    session.flush()
    tr = trailing_revenue_as_of(session, isin=RELIANCE, consolidation=CONS, as_of=NOW)
    assert tr.revenue_inr == D("4600") * CR


def test_other_isin_not_mixed_in(session):
    _load(session)
    assert trailing_revenue_as_of(
        session, isin="INE009A01021", consolidation=CONS, as_of=NOW
    ) is None
