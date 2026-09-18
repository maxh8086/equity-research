"""XBRL results in the database: drop-folder adapter, ISIN resolution at `as_of`, reruns, reparse and review."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    Consolidation,
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFiling,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    NseBhavcopyRow,
    XbrlIsinBasis,
    XbrlTaxonomy,
)
from core.db.models import FinancialFactsQuarantineReason as Reason
from core.db.pit import facts_as_of, financial_facts_quarantine_review_as_of, financial_filings_as_of
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_xbrl import adapters
from ingest.nse_xbrl.adapters import NseXbrlResultsDrop
from ingest.nse_xbrl.mapping import RULE_VERSION
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "nse_xbrl"
ARCHIVE = "https://nsearchives.nseindia.com/corporate/xbrl/"
NAME = "nse_xbrl_results_drop"

INFY, HDFCBANK, OTHER = "INE009A01021", "INE040A01034", "INE002A01018"
INFY_Q2 = "INDAS_112850_1276017_17102024074402.xml"
INFY_Q2_PUBLISHED = datetime(2024, 10, 17, 19, 44, 31, tzinfo=IST)
HDFCBANK_Q3 = "BANKING_117524_1359008_23012025122553.xml"
HDFCBANK_Q3_PUBLISHED = datetime(2025, 1, 23, 12, 26, 7, tzinfo=IST)


def _ctx(session, tmp_path, now, blob=None) -> AdapterContext:
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: True})
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _drop(tmp_path: Path, name: str, published_at: datetime, data: bytes | None = None) -> None:
    folder = tmp_path / NAME
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data if data is not None else (FIXTURES / name).read_bytes())
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": ARCHIVE + name, "published_at": published_at.isoformat(),
                    "media_type": "application/xml"})  # fmt: skip
    )


def _bhav(session, symbol: str, isin: str, day: date) -> None:
    session.add(
        NseBhavcopyRow(
            trade_date=day, isin=isin, symbol=symbol, series="EQ", open=D(1), high=D(1), low=D(1), close=D(1),
            prev_close=D(1), volume=1, turnover=D(1), trades=1, rule_version="t",
            as_of=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=IST),
            content_hash=content_hash(f"{symbol}{isin}{day}".encode()), source_url=f"bhav-{day}",
            extracted_by="tests", model_version=None,
        )  # fmt: skip
    )
    session.flush()


def _index_list(session, symbol: str, isin: str, published: datetime) -> None:
    prov = dict(as_of=published, content_hash=content_hash(f"{symbol}{isin}{published}".encode()),
                source_url=f"list-{published}", extracted_by="tests", model_version=None)  # fmt: skip
    snapshot = IndexSnapshot(index_code=IndexCode.NIFTY_50, constituent_count=1, quarantined_rows=0,
                             rule_version="t", **prov)  # fmt: skip
    session.add(snapshot)
    session.flush()
    session.add(
        IndexSnapshotConstituent(snapshot_id=snapshot.id, isin=isin, symbol=symbol, series="EQ", company_name="C",
                                 industry="I", row_number=1, **prov)  # fmt: skip
    )
    session.flush()


def _quarantine(session) -> list[FinancialFactsQuarantine]:
    return list(session.scalars(select(FinancialFactsQuarantine).order_by(FinancialFactsQuarantine.id)))


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_bank_file_loads_under_its_own_isin(session, tmp_path):
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED)
    result = NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.rows_written, result.quarantined) == (82, 0)

    (filing,) = financial_filings_as_of(session, isin=HDFCBANK, as_of=HDFCBANK_Q3_PUBLISHED)
    assert (filing.isin_basis, filing.taxonomy, filing.consolidation) == (
        XbrlIsinBasis.FILING, XbrlTaxonomy.BANK, Consolidation.STANDALONE,
    )  # fmt: skip
    assert (filing.facts_written, filing.dimensional_facts_deferred, filing.rule_version) == (82, 52, RULE_VERSION)
    assert filing.source_url == ARCHIVE + HDFCBANK_Q3

    profit = facts_as_of(session, isin=HDFCBANK, consolidation=Consolidation.STANDALONE,
                         as_of=HDFCBANK_Q3_PUBLISHED, line_items=["profit_loss_for_the_period"])  # fmt: skip
    quarter = {(f.period_start, f.period_end): f for f in profit}[(date(2024, 10, 1), date(2024, 12, 31))]
    assert quarter.value == D("167355000000.00") and quarter.unit == "INR"
    assert quarter.as_of == HDFCBANK_Q3_PUBLISHED and quarter.model_version is None
    assert quarter.xbrl_element == "in-bse-fin-bank-2019:ProfitLossForThePeriod"


def test_facts_are_invisible_before_publication(session, tmp_path):
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1)))
    before = HDFCBANK_Q3_PUBLISHED - timedelta(seconds=1)
    assert facts_as_of(session, isin=HDFCBANK, consolidation=Consolidation.STANDALONE, as_of=before) == []
    assert financial_filings_as_of(session, isin=HDFCBANK, as_of=before) == []


def test_symbol_resolves_through_recent_bhavcopy(session, tmp_path):
    _bhav(session, "INFY", INFY, date(2024, 10, 16))
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    result = NseXbrlResultsDrop().run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1)))
    assert (result.status, result.rows_written, result.quarantined) == (RunStatus.SUCCEEDED, 225, 2), result.detail
    (filing,) = financial_filings_as_of(session, as_of=INFY_Q2_PUBLISHED)
    assert (filing.isin, filing.isin_basis, filing.symbol) == (INFY, XbrlIsinBasis.BHAVCOPY, "INFY")
    conflicts = _quarantine(session)
    assert {q.reason for q in conflicts} == {Reason.CONFLICTING_VALUES}
    assert {q.isin for q in conflicts} == {INFY} and {q.context_ref for q in conflicts} == {"FourD"}


def test_symbol_resolves_through_an_index_list(session, tmp_path):
    _index_list(session, "INFY", INFY, datetime(2024, 9, 1, 10, tzinfo=IST))
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1)))
    (filing,) = financial_filings_as_of(session, as_of=INFY_Q2_PUBLISHED)
    assert (filing.isin, filing.isin_basis) == (INFY, XbrlIsinBasis.INDEX_LIST)


@pytest.mark.parametrize(
    "seed",
    [
        lambda s: _bhav(s, "INFY", INFY, date(2024, 9, 30)),  # stale: 17 days before publication
        lambda s: _bhav(s, "INFY", INFY, date(2024, 10, 18)),  # traded after publication
        lambda s: _index_list(s, "INFY", INFY, datetime(2024, 3, 1, tzinfo=IST)),  # list too old
        lambda s: _index_list(s, "INFY", INFY, datetime(2024, 10, 18, tzinfo=IST)),  # list not yet public
    ],
    ids=["stale-bhavcopy", "future-bhavcopy", "old-list", "future-list"],
)
def test_no_isin_known_at_publication_is_unresolved(session, tmp_path, seed):
    seed(session)
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    result = NseXbrlResultsDrop().run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=3)))
    assert result.status is RunStatus.FAILED
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_UNRESOLVED and entry.xbrl_element is None and entry.isin is None
    assert session.scalar(select(func.count()).select_from(FinancialFact)) == 0


def test_disagreeing_isin_reads_are_a_conflict(session, tmp_path):
    _bhav(session, "INFY", INFY, date(2024, 10, 16))
    _index_list(session, "INFY", OTHER, datetime(2024, 9, 1, tzinfo=IST))
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1)))
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_CONFLICT and INFY in entry.detail and OTHER in entry.detail


def test_file_isin_must_agree_with_bhavcopy(session, tmp_path):
    _bhav(session, "HDFCBANK", OTHER, date(2025, 1, 22))
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1)))
    assert [q.reason for q in _quarantine(session)] == [Reason.ISIN_CONFLICT]


def test_invalid_file_isin_is_rejected(session, tmp_path):
    data = (FIXTURES / HDFCBANK_Q3).read_bytes().replace(HDFCBANK.encode(), b"INE040A01035")
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED, data)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1)))
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_CONFLICT and "not a valid ISIN" in entry.detail


@pytest.mark.parametrize(
    "published",
    [datetime(2025, 1, 21, 20, tzinfo=IST), datetime(2024, 12, 31, 20, tzinfo=IST)],
    ids=["before-board-meeting", "within-the-period"],
)
def test_implausible_publication_time_is_rejected(session, tmp_path, published):
    _drop(tmp_path, HDFCBANK_Q3, published)
    result = NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED))
    assert result.status is RunStatus.FAILED
    assert [q.reason for q in _quarantine(session)] == [Reason.IMPLAUSIBLE_AS_OF]


def test_unsupported_taxonomy_quarantines_the_file(session, tmp_path):
    data = (FIXTURES / HDFCBANK_Q3).read_bytes().replace(b"banking_entry_point_2019-09-30.xsd", b"banking_2016.xsd")
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED, data)
    NseXbrlResultsDrop().run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1)))
    assert [q.reason for q in _quarantine(session)] == [Reason.UNSUPPORTED_TAXONOMY]


def test_disabled_adapter_writes_nothing(session, tmp_path):
    ctx = _ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED)
    ctx.settings = Settings(drop_folder=tmp_path, source_switches={})
    assert NseXbrlResultsDrop().run(ctx).status is RunStatus.DISABLED


def test_canary_parses_pending_files(session, tmp_path):
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED, b"<not xbrl/>")
    with pytest.raises(Exception, match="shape_changed"):
        NseXbrlResultsDrop().canary(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED))


# --------------------------------------------------------------------------- #
# Reruns, reparse and review
# --------------------------------------------------------------------------- #


def test_rerun_skips_a_loaded_file(session, tmp_path):
    _drop(tmp_path, HDFCBANK_Q3, HDFCBANK_Q3_PUBLISHED)
    adapter, blob = NseXbrlResultsDrop(), MemoryBlobStore()
    adapter.run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=1), blob))
    again = adapter.run(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=2), blob))
    assert (again.rows_written, again.quarantined) == (0, 0) and "1 files already loaded" in again.detail
    reparsed = adapter.reparse(_ctx(session, tmp_path, HDFCBANK_Q3_PUBLISHED + timedelta(days=2), blob))
    assert reparsed.rows_written == 0
    assert session.scalar(select(func.count()).select_from(FinancialFiling)) == 1


def test_unresolved_file_loads_once_its_isin_becomes_known(session, tmp_path):
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    adapter, blob = NseXbrlResultsDrop(), MemoryBlobStore()
    later = INFY_Q2_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1), blob))
    again = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert again.quarantined == 0 and len(_quarantine(session)) == 1  # recorded once
    review = financial_facts_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=later)
    assert [e.needs_review for e in review] == [True]

    _bhav(session, "INFY", INFY, date(2024, 10, 16))  # bhavcopy loaded late, dated as published
    retried = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert (retried.status, retried.rows_written) == (RunStatus.SUCCEEDED, 225), retried.detail
    (filing,) = financial_filings_as_of(session, as_of=later)
    assert filing.as_of == INFY_Q2_PUBLISHED  # the file's publication time, not the retry time
    review = financial_facts_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=later)
    whole_file = [e for e in review if e.row.xbrl_element is None]
    assert [e.needs_review for e in whole_file] == [False]


def test_reparse_under_a_new_rule_adds_rows_and_reviews_old_quarantine(session, tmp_path, monkeypatch):
    _bhav(session, "INFY", INFY, date(2024, 10, 16))
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    adapter, blob = NseXbrlResultsDrop(), MemoryBlobStore()
    later = INFY_Q2_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1), blob))

    monkeypatch.setattr(adapters, "RULE_VERSION", "nse-xbrl-test-2")
    result = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert (result.rows_written, result.quarantined) == (225, 2), result.detail
    filings = financial_filings_as_of(session, isin=INFY, as_of=later)
    assert [f.rule_version for f in filings] == [RULE_VERSION, "nse-xbrl-test-2"]
    assert {f.as_of for f in filings} == {INFY_Q2_PUBLISHED}

    facts = facts_as_of(session, isin=INFY, consolidation=Consolidation.STANDALONE, as_of=later)
    assert len(facts) == 225 and {f.rule_version for f in facts} == {"nse-xbrl-test-2"}

    review = financial_facts_quarantine_review_as_of(session, current_rule_version="nse-xbrl-test-2", as_of=later)
    old = [e for e in review if e.row.rule_version == RULE_VERSION]
    assert len(old) == 2 and all(e.needs_review for e in old)  # the new rule quarantined them again


def test_fact_quarantine_is_resolved_when_a_new_rule_parses_it(session, tmp_path, monkeypatch):
    _bhav(session, "INFY", INFY, date(2024, 10, 16))
    _drop(tmp_path, INFY_Q2, INFY_Q2_PUBLISHED)
    adapter, blob = NseXbrlResultsDrop(), MemoryBlobStore()
    later = INFY_Q2_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, INFY_Q2_PUBLISHED + timedelta(days=1), blob))

    monkeypatch.setattr(adapters, "RULE_VERSION", "nse-xbrl-test-2")
    real_parse = adapters.parse_filing

    def parse_without_conflicts(data):
        parsed = real_parse(data)
        return parsed.__class__(**{**parsed.__dict__, "issues": ()})

    monkeypatch.setattr(adapters, "parse_filing", parse_without_conflicts)
    adapter.reparse(_ctx(session, tmp_path, later, blob))
    review = financial_facts_quarantine_review_as_of(session, current_rule_version="nse-xbrl-test-2", as_of=later)
    assert len(review) == 2 and not any(e.needs_review for e in review)


# --------------------------------------------------------------------------- #
# Database guards
# --------------------------------------------------------------------------- #


def _filing(**overrides) -> FinancialFiling:
    fields = dict(
        isin=INFY, isin_basis=XbrlIsinBasis.BHAVCOPY, symbol="INFY", scrip_code="500209",
        consolidation=Consolidation.STANDALONE, taxonomy=XbrlTaxonomy.IND_AS, reporting_quarter="Half yearly",
        period_start=date(2024, 7, 1), period_end=date(2024, 9, 30), board_meeting_date=date(2024, 10, 17),
        facts_written=1, facts_quarantined=0, dimensional_facts_deferred=0, rule_version="t",
        as_of=INFY_Q2_PUBLISHED, content_hash=content_hash(b"filing"), source_url=ARCHIVE,
        extracted_by="tests", model_version=None,
    )  # fmt: skip
    return FinancialFiling(**(fields | overrides))


@pytest.mark.parametrize(
    "row",
    [
        _filing(as_of=datetime(2024, 10, 16, 20, tzinfo=IST)),
        _filing(as_of=datetime(2024, 9, 30, 20, tzinfo=IST), board_meeting_date=date(2024, 9, 30)),
        _filing(isin="INFY"),
        _filing(facts_written=-1),
        _filing(model_version="claude-sonnet-5"),
        FinancialFactsQuarantine(
            isin=None, xbrl_element="x", context_ref=None, raw_value=None, reason=Reason.UNMAPPED_ELEMENT,
            detail="d", rule_version="t", as_of=INFY_Q2_PUBLISHED, content_hash=content_hash(b"q"),
            source_url=ARCHIVE, extracted_by="tests", model_version=None,
        ),
    ],
    ids=["before-board-meeting", "within-period", "isin-format", "negative-count", "model-version", "half-fact"],
)  # fmt: skip
def test_db_constraints(session, row):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(row)
            session.flush()
