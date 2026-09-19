"""The Screener comparison read at `t`: facts, ratios and the newest export, through core/db/pit.py."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from core.compute import ratios as r
from core.compute import screener_compare as sc
from core.compute.hashing import content_hash
from core.compute.screener_compare import Status
from core.db.models import (
    Consolidation,
    FinancialFiling,
    IndexSnapshot,
    IndexSnapshotConstituent,
    XbrlIsinBasis,
    XbrlTaxonomy,
)
from core.timezones import IST
from ingest.base import RunStatus
from ingest.nse_indices.parser import parse_constituent_list
from ingest.screener_export.adapters import ScreenerExportDrop
from ingest.screener_export.parser import parse_export
from tests.factories import make_fact
from tests.test_screener_export_adapters import EXPORTED, FIXTURE, VINATI, _ctx, _drop, _index_list
from tests.test_validate_samples import LISTS
from validate.__main__ import check_sample
from validate.report import company_report, render_company, render_findings
from validate.samples import SAMPLE_V1

pytestmark = pytest.mark.db

FILED = datetime(2022, 5, 10, 16, tzinfo=IST)
RESTATED = datetime(2023, 5, 10, 16, tzinfo=IST)
FY22 = date(2022, 3, 31)


def _filing(session, name: str, as_of: datetime) -> str:
    digest = content_hash(f"{name}{as_of}".encode())
    session.add(
        FinancialFiling(
            isin=VINATI, isin_basis=XbrlIsinBasis.BHAVCOPY, symbol="VINATIORGA", scrip_code=None,
            consolidation=Consolidation.CONSOLIDATED, taxonomy=XbrlTaxonomy.IND_AS, reporting_quarter="Q4",
            period_start=date(as_of.year, 1, 1), period_end=date(as_of.year, 3, 31),
            board_meeting_date=as_of.date(), facts_written=1, facts_quarantined=0, dimensional_facts_deferred=0,
            rule_version="tests-1", as_of=as_of, content_hash=digest, source_url=f"https://example.test/{name}.xml",
            extracted_by="tests", model_version=None,
        )  # fmt: skip
    )
    return digest


def _fact(session, digest, as_of, item, start, end, value) -> None:
    session.add(make_fact(isin=VINATI, line_item=item, xbrl_element=f"t:{item}", period_start=start,
                          period_end=end, value=value, as_of=as_of, content_hash=digest))  # fmt: skip


def _facts_matching_screener(session) -> None:
    """Our facts for every fiscal year in the fixture, built from Screener's own lines so they agree."""
    values = {(v.statement, v.line, v.period_end): v.value for v in parse_export(FIXTURE.read_bytes()).values}
    digest = _filing(session, "fy22", FILED)
    for end in sorted({k[2] for k in values if k[2].month == 3 and k[2].day == 31}):
        y = sc.screener_year(values, end)
        if "sales" not in y:
            continue
        start = r.shift_years(end, -1) + timedelta(days=1)
        duration = {
            r.REVENUE: y["sales"], r.TOTAL_EXPENSES: y["sales"] - y["operating_profit"] + y["interest"] + y["depreciation"],
            r.FINANCE_COSTS: y["interest"], r.DEPRECIATION: y["depreciation"], "other_income": y["other_income"],
            r.PROFIT_BEFORE_TAX: y["pbt"], r.PROFIT: y["net_profit"],
            "profit_or_loss_attributable_to_owners_of_parent": y["net_profit"],
        }  # fmt: skip
        instant = {r.EQUITY: y["equity"], r.BORROWINGS_NONCURRENT: y["borrowings"], r.BORROWINGS_CURRENT: Decimal(0),
                   r.CASH: y["cash_bank"], r.OTHER_BANK_BALANCES: Decimal(0), r.CURRENT_INVESTMENTS: Decimal(0),
                   "capital_work_in_progress": y["cwip"]}  # fmt: skip
        for item, value in duration.items():
            _fact(session, digest, FILED, item, start, end, value)
        for item, value in instant.items():
            _fact(session, digest, FILED, item, None, end, value)
    session.flush()


@pytest.fixture
def loaded(session, tmp_path):
    _index_list(session, "VINATIORGA", VINATI)
    _drop(tmp_path)
    result = ScreenerExportDrop().run(_ctx(session, tmp_path, EXPORTED + timedelta(hours=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    _facts_matching_screener(session)
    return session


def _report(session, as_of):
    return company_report(session, isin=VINATI, symbol="VINATIORGA", years=[2022], as_of=as_of)


def test_agreeing_numbers_pass_the_gate(loaded):
    rep = _report(loaded, EXPORTED + timedelta(days=1))
    assert rep.family == "ind_as" and not rep.problems
    failing = [(c.name, c.status) for c in rep.comparisons if c.gate and c.status is not Status.MATCH]
    assert failing == []
    assert rep.passes
    assert {c.name for c in rep.comparisons if c.kind == "ratio"} >= {str(r.Ratio.ROCE), str(r.Ratio.INCREMENTAL_ROCE)}


def test_a_restatement_known_later_diverges_only_from_then(loaded):
    digest = _filing(loaded, "fy23", RESTATED)
    start = date(2021, 4, 1)
    _fact(loaded, digest, RESTATED, r.REVENUE, start, FY22, Decimal("1700") * sc.CRORE)
    loaded.flush()

    assert _report(loaded, RESTATED - timedelta(seconds=1)).passes
    rep = _report(loaded, RESTATED)
    sales = next(c for c in rep.comparisons if c.name == "sales")
    assert sales.status is Status.DIVERGE and not rep.passes
    text = "\n".join(render_company(rep, show_all=False))
    assert "FAIL" in text and "revenue_from_operations = 1,700.00 cr" in text


def test_nothing_known_is_a_failure_with_reasons(session):
    rep = _report(session, EXPORTED)
    assert not rep.passes
    assert rep.problems == ["no Screener export known at this as_of", "no consolidated XBRL filing known at this as_of"]


def test_findings_classify_nci(loaded):
    rep = _report(loaded, EXPORTED + timedelta(days=1))
    text = "\n".join(render_findings([rep]))
    assert "net_profit: both (no NCI to tell apart) 1" in text


# --------------------------------------------------------------------------- #
# python -m validate sample
# --------------------------------------------------------------------------- #


def _load_recorded_lists(session, published: datetime) -> None:
    """The two constituent lists SAMPLE_V1 records, as snapshots with the recorded file hashes."""
    for d in SAMPLE_V1.draws:
        data = LISTS[d.index_code].read_bytes()
        assert content_hash(data) == d.list_content_hash
        prov = dict(as_of=published, content_hash=d.list_content_hash, source_url=f"list-{d.index_code}",
                    extracted_by="tests", model_version=None)  # fmt: skip
        rows = parse_constituent_list(data, d.index_code).constituents
        snapshot = IndexSnapshot(index_code=d.index_code, constituent_count=len(rows), quarantined_rows=0,
                                 rule_version="t", **prov)  # fmt: skip
        session.add(snapshot)
        session.flush()
        session.add_all(
            IndexSnapshotConstituent(snapshot_id=snapshot.id, isin=c.row.isin, symbol=c.row.symbol, series="EQ",
                                     company_name="C", industry="I", row_number=n, **prov)  # fmt: skip
            for n, c in enumerate(rows, 1)
        )
    session.flush()


def test_sample_draw_reproduces_from_the_recorded_lists(session, capsys):
    published = SAMPLE_V1.membership_on - timedelta(days=4)
    assert check_sample(session, SAMPLE_V1, SAMPLE_V1.membership_on) == 1  # lists not loaded yet
    _load_recorded_lists(session, published)
    assert check_sample(session, SAMPLE_V1, SAMPLE_V1.membership_on) == 0
    assert "SBILIFE" in capsys.readouterr().out
    # Seen before the lists were published, the draw cannot be checked.
    assert check_sample(session, SAMPLE_V1, published - timedelta(seconds=1)) == 1
