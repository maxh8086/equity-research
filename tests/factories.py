from datetime import date, datetime
from decimal import Decimal

from core.compute.hashing import content_hash
from core.db.models import Consolidation, FactKind, FinancialFact
from core.timezones import IST

RELIANCE = "INE002A01018"


def make_fact(**overrides) -> FinancialFact:
    fields = dict(
        isin=RELIANCE,
        consolidation=Consolidation.CONSOLIDATED,
        fact_kind=FactKind.REPORTED,
        line_item="revenue_from_operations",
        xbrl_element="in-bse-fin:RevenueFromOperations",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
        value=Decimal("2360000000000.00"),
        unit="INR",
        rule_version="tests-1",
        as_of=datetime(2024, 4, 22, 16, 30, tzinfo=IST),
        content_hash=content_hash(b"filing-bytes"),
        source_url="https://www.bseindia.com/example.xml",
        extracted_by="tests.factories",
        model_version=None,
    )
    fields.update(overrides)
    return FinancialFact(**fields)
