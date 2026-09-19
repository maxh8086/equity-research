"""Ratios read at `t` from financial_facts: point in time, with reasons for every gap."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.compute.hashing import content_hash
from core.compute.ratios import RULE_VERSION, Ratio
from core.config import Settings
from core.db.models import Consolidation, FinancialFiling, XbrlIsinBasis, XbrlTaxonomy
from core.db.pit import RatioGap, ratios_as_of
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_xbrl.adapters import NseXbrlResultsDrop
from tests.factories import RELIANCE, make_fact
from tests.fakes import MemoryBlobStore
from tests.test_nse_xbrl_adapters import _bhav

pytestmark = pytest.mark.db

D = Decimal
CR = D("10000000")  # one crore, in rupees
CONS = Consolidation.CONSOLIDATED
FY24 = (date(2023, 4, 1), date(2024, 3, 31))
FY21 = (date(2020, 4, 1), date(2021, 3, 31))
Q4 = (date(2024, 1, 1), date(2024, 3, 31))
FILED = datetime(2024, 4, 22, 16, 30, tzinfo=IST)


def _filing(session, name: str, as_of: datetime, period: tuple[date, date], taxonomy=XbrlTaxonomy.IND_AS) -> str:
    """A financial_filing row; returns its content hash for the facts that came from it."""
    digest = content_hash(f"{name}{as_of}".encode())
    session.add(
        FinancialFiling(
            isin=RELIANCE, isin_basis=XbrlIsinBasis.BHAVCOPY, symbol="RELIANCE", scrip_code=None,
            consolidation=CONS, taxonomy=taxonomy, reporting_quarter="Q4", period_start=period[0],
            period_end=period[1], board_meeting_date=as_of.date(), facts_written=1, facts_quarantined=0,
            dimensional_facts_deferred=0, rule_version="tests-1", as_of=as_of, content_hash=digest,
            source_url=f"https://example.test/{name}.xml", extracted_by="tests", model_version=None,
        )  # fmt: skip
    )
    return digest


def _facts(session, digest: str, as_of: datetime, period: tuple[date, date] | None, end: date, **items) -> None:
    start = period[0] if period else None
    end = period[1] if period else end
    session.add_all(
        make_fact(line_item=item, xbrl_element=f"t:{item}", period_start=start, period_end=end,
                  value=D(value) * CR, as_of=as_of, content_hash=digest)  # fmt: skip
        for item, value in items.items()
    )
    session.flush()


def _pnl(session, digest, as_of, period, **overrides) -> None:
    items = dict(revenue_from_operations=1000, expenses=800, finance_costs=20, profit_before_tax=200,
                 depreciation_depletion_and_amortisation_expense=50, profit_loss_for_period=150)  # fmt: skip
    _facts(session, digest, as_of, period, period[1], **(items | overrides))


def _balance(session, digest, as_of, end, **overrides) -> None:
    items = dict(equity=900, borrowings_current=100, borrowings_noncurrent=200, cash_and_cash_equivalents=50,
                 bank_balance_other_than_cash_and_cash_equivalents=10, current_investments=40)  # fmt: skip
    _facts(session, digest, as_of, None, end, **(items | overrides))


def _read(session, as_of) -> dict[tuple[Ratio, date | None, date], object]:
    return {(v.ratio, v.period_start, v.period_end): v for v in ratios_as_of(
        session, isin=RELIANCE, consolidation=CONS, as_of=as_of)}  # fmt: skip


@pytest.fixture
def fy24(session):
    """FY24 year-end filing plus the FY23 year-end balance sheet a year earlier."""
    fy23 = _filing(session, "fy23", datetime(2023, 4, 21, 16, tzinfo=IST), (date(2023, 1, 1), date(2023, 3, 31)))
    _balance(session, fy23, datetime(2023, 4, 21, 16, tzinfo=IST), date(2023, 3, 31), equity=700)
    digest = _filing(session, "fy24", FILED, Q4)
    _pnl(session, digest, FILED, FY24)
    _pnl(session, digest, FILED, Q4, revenue_from_operations=300, expenses=240, profit_before_tax=60,
         finance_costs=5, depreciation_depletion_and_amortisation_expense=10, profit_loss_for_period=45)  # fmt: skip
    _balance(session, digest, FILED, date(2024, 3, 31))
    return digest


def test_margins_for_every_reported_period(session, fy24):
    ratios = _read(session, FILED)
    # (1000 - (800 - 20 - 50)) / 1000
    assert ratios[(Ratio.OPERATING_MARGIN, *FY24)].value == D("0.27")
    assert ratios[(Ratio.EBIT_MARGIN, *FY24)].value == D("0.22")
    assert ratios[(Ratio.NET_MARGIN, *FY24)].value == D("0.15")
    assert ratios[(Ratio.INTEREST_COVERAGE, *FY24)].value == D(11)
    # (300 - (240 - 5 - 10)) / 300
    assert ratios[(Ratio.OPERATING_MARGIN, *Q4)].value == D("0.25")
    value = ratios[(Ratio.NET_MARGIN, *Q4)]
    assert (value.gap, value.rule_version, value.known_since) == (None, RULE_VERSION, FILED)
    assert {f.line_item for f in value.inputs} == {"profit_loss_for_period", "revenue_from_operations"}


def test_debt_ratios_for_every_balance_sheet_date(session, fy24):
    ratios = _read(session, FILED)
    assert ratios[(Ratio.DEBT_TO_EQUITY, None, date(2024, 3, 31))].value == D(300) / D(900)
    assert ratios[(Ratio.NET_DEBT_TO_EQUITY, None, date(2024, 3, 31))].value == D(200) / D(900)
    assert ratios[(Ratio.DEBT_TO_EQUITY, None, date(2023, 3, 31))].value == D(300) / D(700)


def test_roce_uses_the_opening_and_closing_balance_sheets(session, fy24):
    roce = _read(session, FILED)[(Ratio.ROCE, *FY24)]
    # EBIT 220 over average capital employed (1,000 + 1,200) / 2
    assert roce.value == D("0.2")
    assert len(roce.inputs) == 8
    # Only fiscal years get ROCE; the quarter does not.
    assert (Ratio.ROCE, *Q4) not in _read(session, FILED)


def test_roce_without_an_opening_balance_sheet_names_what_is_missing(session):
    digest = _filing(session, "fy24", FILED, Q4)
    _pnl(session, digest, FILED, FY24)
    _balance(session, digest, FILED, date(2024, 3, 31))
    roce = _read(session, FILED)[(Ratio.ROCE, *FY24)]
    assert (roce.value, roce.gap) == (None, RatioGap.MISSING_INPUT)
    assert set(roce.missing) == {
        "equity @2023-03-31", "borrowings_current @2023-03-31", "borrowings_noncurrent @2023-03-31",
    }  # fmt: skip


def test_incremental_roce_against_three_years_earlier(session, fy24):
    fy21_at = datetime(2021, 5, 1, tzinfo=IST)
    digest = _filing(session, "fy21", fy21_at, (date(2021, 1, 1), date(2021, 3, 31)))
    _pnl(session, digest, fy21_at, FY21, profit_before_tax=100, finance_costs=10)
    _balance(session, digest, fy21_at, date(2021, 3, 31), equity=500, borrowings_current=50, borrowings_noncurrent=50)
    ratios = _read(session, FILED)
    # EBIT 110 -> 220 while capital employed 600 -> 1,200
    assert ratios[(Ratio.INCREMENTAL_ROCE, *FY24)].value == D(110) / D(600)
    earliest = ratios[(Ratio.INCREMENTAL_ROCE, *FY21)]
    assert earliest.gap is RatioGap.MISSING_INPUT
    assert "profit_before_tax 2017-04-01..2018-03-31" in earliest.missing


def test_a_restatement_changes_ratios_only_from_when_it_was_known(session, fy24):
    restated_at = FILED + timedelta(days=90)
    digest = _filing(session, "fy24-restated", restated_at, Q4)
    _facts(session, digest, restated_at, FY24, FY24[1], profit_loss_for_period=100)
    assert _read(session, FILED)[(Ratio.NET_MARGIN, *FY24)].value == D("0.15")
    later = _read(session, restated_at)[(Ratio.NET_MARGIN, *FY24)]
    assert (later.value, later.known_since) == (D("0.1"), restated_at)


def test_nothing_is_known_before_the_filing(session, fy24):
    ratios = _read(session, FILED - timedelta(seconds=1))
    assert all(period_end < date(2024, 1, 1) for _, _, period_end in ratios)


def test_financial_companies_are_not_applicable(session):
    digest = _filing(session, "nbfc", FILED, Q4, taxonomy=XbrlTaxonomy.NBFC)
    _pnl(session, digest, FILED, FY24)
    _balance(session, digest, FILED, date(2024, 3, 31))
    ratios = _read(session, FILED)
    assert ratios and {v.gap for v in ratios.values()} == {RatioGap.NOT_APPLICABLE}
    assert all(v.value is None for v in ratios.values())


def test_facts_without_a_known_filing_are_not_applicable(session):
    _pnl(session, content_hash(b"orphan"), FILED, FY24)
    assert {v.gap for v in _read(session, FILED).values()} == {RatioGap.NOT_APPLICABLE}


def test_zero_denominators_are_undefined_and_bad_inputs_are_flagged(session):
    digest = _filing(session, "odd", FILED, Q4)
    _pnl(session, digest, FILED, FY24, revenue_from_operations=0, finance_costs=0)
    _pnl(session, digest, FILED, Q4, finance_costs=-5)
    ratios = _read(session, FILED)
    assert ratios[(Ratio.NET_MARGIN, *FY24)].gap is RatioGap.UNDEFINED
    assert ratios[(Ratio.INTEREST_COVERAGE, *FY24)].gap is RatioGap.UNDEFINED
    assert ratios[(Ratio.INTEREST_COVERAGE, *Q4)].gap is RatioGap.INVALID_INPUT
    assert ratios[(Ratio.NET_MARGIN, *Q4)].gap is None


def test_naive_as_of_raises(session):
    with pytest.raises(ValueError):
        ratios_as_of(session, isin=RELIANCE, consolidation=CONS, as_of=datetime(2024, 5, 1))


# --------------------------------------------------------------------------- #
# End to end on a real filing
# --------------------------------------------------------------------------- #

INFY = "INE009A01021"
INFY_FY24 = "INDAS_104589_1099938_19042024112830.xml"
INFY_FY24_PUBLISHED = datetime(2024, 4, 19, 11, 32, 6, tzinfo=IST)
FIXTURES = Path(__file__).parent / "fixtures" / "nse_xbrl"
NAME = "nse_xbrl_results_drop"


def test_infosys_fy24_ratios_from_the_filed_xbrl(session, tmp_path):
    _bhav(session, "INFY", INFY, date(2024, 4, 18))
    folder = tmp_path / NAME
    folder.mkdir()
    (folder / INFY_FY24).write_bytes((FIXTURES / INFY_FY24).read_bytes())
    (folder / f"{INFY_FY24}.meta.json").write_text(json.dumps({
        "source_url": "https://nsearchives.nseindia.com/corporate/xbrl/" + INFY_FY24,
        "published_at": INFY_FY24_PUBLISHED.isoformat(), "media_type": "application/xml",
    }))  # fmt: skip
    ctx = AdapterContext(session, MemoryBlobStore(), Settings(drop_folder=tmp_path, source_switches={NAME: True}),
                         now=lambda: INFY_FY24_PUBLISHED + timedelta(days=1))  # fmt: skip
    assert NseXbrlResultsDrop().run(ctx).status is RunStatus.SUCCEEDED

    ratios = {(v.ratio, v.period_start, v.period_end): v for v in ratios_as_of(
        session, isin=INFY, consolidation=CONS, as_of=INFY_FY24_PUBLISHED)}  # fmt: skip
    # Figures in crore: operating profit 36,425 on revenue 1,53,670; EBIT 36,458; interest 470.
    assert round(ratios[(Ratio.OPERATING_MARGIN, *FY24)].value, 4) == D("0.2370")
    assert round(ratios[(Ratio.EBIT_MARGIN, *FY24)].value, 4) == D("0.2372")
    assert round(ratios[(Ratio.NET_MARGIN, *FY24)].value, 4) == D("0.1708")
    assert round(ratios[(Ratio.INTEREST_COVERAGE, *FY24)].value, 2) == D("77.57")
    # No borrowings reported (leases are not tagged separately); net cash of 27,701 crore.
    assert ratios[(Ratio.DEBT_TO_EQUITY, None, date(2024, 3, 31))].value == 0
    assert round(ratios[(Ratio.NET_DEBT_TO_EQUITY, None, date(2024, 3, 31))].value, 4) == D("-0.3131")
    # One filing has no opening balance sheet: ROCE waits for FY23's.
    assert ratios[(Ratio.ROCE, *FY24)].gap is RatioGap.MISSING_INPUT
    assert ratios_as_of(session, isin=INFY, consolidation=CONS,
                        as_of=INFY_FY24_PUBLISHED - timedelta(seconds=1)) == []  # fmt: skip
