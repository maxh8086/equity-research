"""Shareholding patterns in the database: drop-folder adapter, ISIN resolution at `as_of`, revisions, reparse, review."""

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
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    NseBhavcopyRow,
    ShareholdingFiling,
    ShareholdingPattern,
    ShareholdingQuarantine,
    XbrlIsinBasis,
)
from core.db.models import ShareholdingQuarantineReason as Reason
from core.db.pit import shareholding_filings_as_of, shareholding_pattern_as_of, shareholding_quarantine_review_as_of
from core.timezones import IST
from ingest.base import AdapterContext, RunStatus
from ingest.nse_shp import adapters
from ingest.nse_shp.adapters import NseShpDrop, generated_no_earlier_than
from ingest.nse_shp.mapping import RULE_VERSION
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "nse_shp"
ARCHIVE = "https://nsearchives.nseindia.com/corporate/xbrl/"
NAME = "nse_shareholding_drop"

VEDL, ADANIPORTS, OTHER = "INE205A01025", "INE742F01042", "INE002A01018"
AS_ON = date(2026, 6, 30)
# Publication times assumed for the tests: after the file name's clock read as PM (tests/fixtures/nse_shp/SOURCES.md).
VEDL_FILE = "SHP_1696479_20072026120029_WEB.xml"
VEDL_PUBLISHED = datetime(2026, 7, 20, 12, 5, tzinfo=IST)
PORTS_FILE = "SHP_1690812_10072026084030_WEB.xml"
PORTS_PUBLISHED = datetime(2026, 7, 10, 20, 45, tzinfo=IST)
PORTS_ISIN_FACT = b'<in-bse-shp:ISIN contextRef="MainD">INE742F01042<'


def _ctx(session, tmp_path, now, blob=None) -> AdapterContext:
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: True})
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _drop(tmp_path: Path, name: str, published_at: datetime, data: bytes | None = None, url_name: str | None = None):
    folder = tmp_path / NAME
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(data if data is not None else (FIXTURES / name).read_bytes())
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": ARCHIVE + (url_name or name), "published_at": published_at.isoformat(),
                    "media_type": "application/xml"})  # fmt: skip
    )


def _ports_without_isin() -> bytes:
    data = (FIXTURES / PORTS_FILE).read_bytes()
    assert data.count(PORTS_ISIN_FACT) == 1
    return data.replace(PORTS_ISIN_FACT, b'<in-bse-shp:ISIN contextRef="MainD"><')


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


def _quarantine(session) -> list[ShareholdingQuarantine]:
    return list(session.scalars(select(ShareholdingQuarantine).order_by(ShareholdingQuarantine.id)))


def _values(rows: list[ShareholdingPattern]) -> dict[tuple[str, str], int]:
    return {(r.category, r.measure): r.value for r in rows}


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_file_loads_under_its_own_isin(session, tmp_path):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    result = NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.rows_written, result.quarantined) == (377, 0)

    (filing,) = shareholding_filings_as_of(session, isin=VEDL, as_of=VEDL_PUBLISHED)
    assert (filing.isin_basis, filing.symbol, filing.scrip_code, filing.as_on_date) == (
        XbrlIsinBasis.FILING, "VEDL", "500295", AS_ON,
    )  # fmt: skip
    assert (filing.taxonomy_version, filing.rows_written, filing.facts_quarantined) == ("2025-10-31", 377, 0)
    assert (filing.typed_facts_deferred, filing.percentage_facts_skipped) == (599, 143)
    assert (filing.rule_version, filing.source_url, filing.model_version) == (RULE_VERSION, ARCHIVE + VEDL_FILE, None)

    pattern = shareholding_pattern_as_of(session, isin=VEDL, as_of=VEDL_PUBLISHED)
    assert list(pattern) == [AS_ON]
    rows = pattern[AS_ON]
    assert len(rows) == 377 and all(r.as_of == VEDL_PUBLISHED for r in rows)
    values = _values(rows)
    assert values[("promoter_group", "total_shares")] == 2_139_794_759
    assert values[("promoter_group", "encumbered_shares")] == 2_139_651_763
    (promoter,) = [r for r in rows if (r.category, r.measure) == ("promoter_group", "total_shares")]
    assert (promoter.parent_category, promoter.xbrl_element, promoter.xbrl_member) == (
        "total", "in-bse-shp:NumberOfShares", "in-bse-shp:ShareholdingOfPromoterAndPromoterGroupMember",
    )  # fmt: skip


def test_pattern_is_invisible_before_publication(session, tmp_path):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    before = VEDL_PUBLISHED - timedelta(seconds=1)
    assert shareholding_pattern_as_of(session, isin=VEDL, as_of=before) == {}
    assert shareholding_filings_as_of(session, isin=VEDL, as_of=before) == []


def test_symbol_resolves_through_recent_bhavcopy(session, tmp_path):
    _bhav(session, "ADANIPORTS", ADANIPORTS, date(2026, 7, 9))
    _drop(tmp_path, PORTS_FILE, PORTS_PUBLISHED, _ports_without_isin())
    result = NseShpDrop().run(_ctx(session, tmp_path, PORTS_PUBLISHED + timedelta(days=1)))
    assert (result.status, result.rows_written, result.quarantined) == (RunStatus.SUCCEEDED, 299, 0), result.detail
    (filing,) = shareholding_filings_as_of(session, as_of=PORTS_PUBLISHED)
    assert (filing.isin, filing.isin_basis, filing.symbol) == (ADANIPORTS, XbrlIsinBasis.BHAVCOPY, "ADANIPORTS")


def test_symbol_resolves_through_an_index_list(session, tmp_path):
    _index_list(session, "ADANIPORTS", ADANIPORTS, datetime(2026, 3, 1, 10, tzinfo=IST))
    _drop(tmp_path, PORTS_FILE, PORTS_PUBLISHED, _ports_without_isin())
    NseShpDrop().run(_ctx(session, tmp_path, PORTS_PUBLISHED + timedelta(days=1)))
    (filing,) = shareholding_filings_as_of(session, as_of=PORTS_PUBLISHED)
    assert (filing.isin, filing.isin_basis) == (ADANIPORTS, XbrlIsinBasis.INDEX_LIST)


@pytest.mark.parametrize(
    "seed",
    [
        lambda s: _bhav(s, "ADANIPORTS", ADANIPORTS, date(2026, 6, 25)),  # stale: 15 days before publication
        lambda s: _bhav(s, "ADANIPORTS", ADANIPORTS, date(2026, 7, 11)),  # traded after publication
        lambda s: _index_list(s, "ADANIPORTS", ADANIPORTS, datetime(2025, 12, 1, tzinfo=IST)),  # list too old
        lambda s: _index_list(s, "ADANIPORTS", ADANIPORTS, datetime(2026, 7, 11, tzinfo=IST)),  # not yet public
    ],
    ids=["stale-bhavcopy", "future-bhavcopy", "old-list", "future-list"],
)
def test_no_isin_known_at_publication_is_unresolved(session, tmp_path, seed):
    seed(session)
    _drop(tmp_path, PORTS_FILE, PORTS_PUBLISHED, _ports_without_isin())
    result = NseShpDrop().run(_ctx(session, tmp_path, PORTS_PUBLISHED + timedelta(days=3)))
    assert result.status is RunStatus.FAILED
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_UNRESOLVED and entry.xbrl_element is None and entry.isin is None
    assert session.scalar(select(func.count()).select_from(ShareholdingPattern)) == 0


def test_file_isin_must_agree_with_bhavcopy(session, tmp_path):
    _bhav(session, "VEDL", OTHER, date(2026, 7, 17))
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_CONFLICT and VEDL in entry.detail and OTHER in entry.detail
    assert session.scalar(select(func.count()).select_from(ShareholdingFiling)) == 0


def test_invalid_file_isin_is_rejected(session, tmp_path):
    data = (FIXTURES / VEDL_FILE).read_bytes().replace(VEDL.encode(), b"INE205A01026")
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED, data)
    NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    (entry,) = _quarantine(session)
    assert entry.reason is Reason.ISIN_CONFLICT and "not a valid ISIN" in entry.detail


@pytest.mark.parametrize(
    "published",
    [datetime(2026, 6, 30, 20, tzinfo=IST), datetime(2026, 7, 19, 20, tzinfo=IST)],
    ids=["on-the-as-on-date", "before-the-file-name-timestamp"],
)
def test_implausible_publication_time_is_rejected(session, tmp_path, published):
    _drop(tmp_path, VEDL_FILE, published)
    result = NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED))
    assert result.status is RunStatus.FAILED
    assert [q.reason for q in _quarantine(session)] == [Reason.IMPLAUSIBLE_AS_OF]


def test_totals_mismatch_quarantines_the_whole_file(session, tmp_path):
    old = b'<in-bse-shp:NumberOfShares contextRef="Indian_ContextI" decimals="INF" unitRef="shares">142996<'
    data = (FIXTURES / VEDL_FILE).read_bytes().replace(old, old.replace(b">142996<", b">142997<"))
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED, data)
    NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    (entry,) = _quarantine(session)
    assert (entry.reason, entry.isin, entry.xbrl_element) == (Reason.TOTALS_MISMATCH, None, None)
    assert session.scalar(select(func.count()).select_from(ShareholdingPattern)) == 0


def test_unmapped_category_is_quarantined_with_the_filing_isin(session, tmp_path):
    old = b">in-bse-shp:BodiesCorporateMember</xbrldi:explicitMember>"
    data = (FIXTURES / VEDL_FILE).read_bytes().replace(old, b">in-bse-shp:NewSebiCategoryMember</xbrldi:explicitMember>")
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED, data)
    result = NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    assert result.status is RunStatus.SUCCEEDED and result.quarantined > 0
    entries = _quarantine(session)
    assert {q.reason for q in entries} == {Reason.UNMAPPED_CATEGORY} and {q.isin for q in entries} == {VEDL}
    assert {q.context_ref for q in entries} == {"BodiesCorporate_ContextI"}
    (filing,) = shareholding_filings_as_of(session, as_of=VEDL_PUBLISHED)
    assert filing.facts_quarantined == len(entries)


def test_revised_filing_replaces_the_original_from_its_own_as_of(session, tmp_path):
    original = (FIXTURES / VEDL_FILE).read_bytes()
    revised = original.replace(b"<!--SHP V1.1 (01-12-2025)-->", b"<!--SHP V1.1 (01-12-2025) revised-->")
    revised_at = VEDL_PUBLISHED + timedelta(days=5)
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    _drop(tmp_path, "revised.xml", revised_at, revised, url_name="SHP_1700001_25072026120000_WEB.xml")
    NseShpDrop().run(_ctx(session, tmp_path, revised_at + timedelta(days=1)))

    assert len(shareholding_filings_as_of(session, isin=VEDL, as_of=revised_at)) == 2
    before = shareholding_pattern_as_of(session, isin=VEDL, as_of=revised_at - timedelta(seconds=1))
    after = shareholding_pattern_as_of(session, isin=VEDL, as_of=revised_at)
    assert {r.content_hash for r in before[AS_ON]} == {content_hash(original)}
    assert {r.content_hash for r in after[AS_ON]} == {content_hash(revised)}  # never a mix
    assert len(after[AS_ON]) == 377
    assert shareholding_pattern_as_of(session, isin=VEDL, as_of=revised_at, as_on_date=date(2026, 3, 31)) == {}


def test_disabled_adapter_writes_nothing(session, tmp_path):
    ctx = _ctx(session, tmp_path, VEDL_PUBLISHED)
    ctx.settings = Settings(drop_folder=tmp_path, source_switches={})
    assert NseShpDrop().run(ctx).status is RunStatus.DISABLED


def test_canary_parses_pending_files(session, tmp_path):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED, b"<not xbrl/>")
    with pytest.raises(Exception, match="shape_changed"):
        NseShpDrop().canary(_ctx(session, tmp_path, VEDL_PUBLISHED))


# --------------------------------------------------------------------------- #
# Reruns, reparse and review
# --------------------------------------------------------------------------- #


def test_rerun_skips_a_loaded_file(session, tmp_path):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    adapter, blob = NseShpDrop(), MemoryBlobStore()
    adapter.run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1), blob))
    again = adapter.run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=2), blob))
    assert (again.rows_written, again.quarantined) == (0, 0) and "1 files already loaded" in again.detail
    reparsed = adapter.reparse(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=2), blob))
    assert reparsed.rows_written == 0
    assert session.scalar(select(func.count()).select_from(ShareholdingFiling)) == 1


def test_unresolved_file_loads_once_its_isin_becomes_known(session, tmp_path):
    _drop(tmp_path, PORTS_FILE, PORTS_PUBLISHED, _ports_without_isin())
    adapter, blob = NseShpDrop(), MemoryBlobStore()
    later = PORTS_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, PORTS_PUBLISHED + timedelta(days=1), blob))
    again = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert again.quarantined == 0 and len(_quarantine(session)) == 1  # recorded once
    review = shareholding_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=later)
    assert [e.needs_review for e in review] == [True]

    _bhav(session, "ADANIPORTS", ADANIPORTS, date(2026, 7, 9))  # bhavcopy loaded late, dated as traded
    retried = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert (retried.status, retried.rows_written) == (RunStatus.SUCCEEDED, 299), retried.detail
    (filing,) = shareholding_filings_as_of(session, as_of=later)
    assert filing.as_of == PORTS_PUBLISHED  # the file's publication time, not the retry time
    review = shareholding_quarantine_review_as_of(session, current_rule_version=RULE_VERSION, as_of=later)
    assert [e.needs_review for e in review] == [False]


def test_reparse_under_a_new_rule_adds_rows_and_reads_take_it(session, tmp_path, monkeypatch):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    adapter, blob = NseShpDrop(), MemoryBlobStore()
    later = VEDL_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1), blob))

    monkeypatch.setattr(adapters, "RULE_VERSION", "nse-shp-test-2")
    result = adapter.reparse(_ctx(session, tmp_path, later, blob))
    assert (result.rows_written, result.quarantined) == (377, 0), result.detail
    filings = shareholding_filings_as_of(session, isin=VEDL, as_of=later)
    assert [f.rule_version for f in filings] == [RULE_VERSION, "nse-shp-test-2"]
    assert {f.as_of for f in filings} == {VEDL_PUBLISHED}
    rows = shareholding_pattern_as_of(session, isin=VEDL, as_of=later)[AS_ON]
    assert len(rows) == 377 and {r.rule_version for r in rows} == {"nse-shp-test-2"}


def test_fact_quarantine_is_resolved_when_a_new_rule_maps_it(session, tmp_path, monkeypatch):
    old = b">in-bse-shp:BodiesCorporateMember</xbrldi:explicitMember>"
    data = (FIXTURES / VEDL_FILE).read_bytes().replace(old, b">in-bse-shp:NewSebiCategoryMember</xbrldi:explicitMember>")
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED, data)
    adapter, blob = NseShpDrop(), MemoryBlobStore()
    later = VEDL_PUBLISHED + timedelta(days=30)
    adapter.run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1), blob))
    held = len(_quarantine(session))

    monkeypatch.setattr(adapters, "RULE_VERSION", "nse-shp-test-2")
    real_parse = adapters.parse_filing

    def parse_with_new_mapping(raw):
        parsed = real_parse(raw)
        return parsed.__class__(**{**parsed.__dict__, "issues": ()})

    monkeypatch.setattr(adapters, "parse_filing", parse_with_new_mapping)
    adapter.reparse(_ctx(session, tmp_path, later, blob))
    review = shareholding_quarantine_review_as_of(session, current_rule_version="nse-shp-test-2", as_of=later)
    assert len(review) == held and not any(e.needs_review for e in review)


# --------------------------------------------------------------------------- #
# File-name clock
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SHP_1690812_10072026084030_WEB.xml", datetime(2026, 7, 10, 8, 40, 30, tzinfo=IST)),
        ("SHP_1696479_20072026120029_WEB.xml", datetime(2026, 7, 20, 0, 0, 29, tzinfo=IST)),  # 12 o'clock may be AM
        ("SHP_1_20072026180029_WEB.xml", datetime(2026, 7, 20, 18, 0, 29, tzinfo=IST)),
        ("SHP_1_32072026080000_WEB.xml", None),
        ("shareholding.xml", None),
    ],
)
def test_file_name_clock_is_read_as_its_earliest_time(name, expected):
    assert generated_no_earlier_than(ARCHIVE + name) == expected


# --------------------------------------------------------------------------- #
# Database guards
# --------------------------------------------------------------------------- #


def _prov(**overrides) -> dict:
    fields = dict(rule_version="t", as_of=VEDL_PUBLISHED, content_hash=content_hash(b"filing"), source_url=ARCHIVE,
                  extracted_by="tests", model_version=None)  # fmt: skip
    return fields | overrides


def _filing(**overrides) -> ShareholdingFiling:
    fields = dict(
        isin=VEDL, isin_basis=XbrlIsinBasis.FILING, symbol="VEDL", scrip_code="500295", as_on_date=AS_ON,
        allotment_date=None, taxonomy_version="2025-10-31", rows_written=1, facts_quarantined=0,
        typed_facts_deferred=0, percentage_facts_skipped=0,
    )  # fmt: skip
    return ShareholdingFiling(**(fields | _prov() | overrides))


def _pattern(**overrides) -> ShareholdingPattern:
    fields = dict(isin=VEDL, as_on_date=AS_ON, category="total", parent_category=None, measure="total_shares",
                  value=1, xbrl_element="in-bse-shp:NumberOfShares", xbrl_member="in-bse-shp:ShareholdingPatternMember")  # fmt: skip
    return ShareholdingPattern(**(fields | _prov() | overrides))


@pytest.mark.parametrize(
    "row",
    [
        _filing(as_of=datetime(2026, 6, 30, 20, tzinfo=IST)),
        _filing(isin="VEDL"),
        _filing(rows_written=-1),
        _filing(model_version="claude-sonnet-5"),
        _pattern(value=-1),
        _pattern(as_of=datetime(2026, 6, 30, 23, tzinfo=IST)),
        _pattern(content_hash="not-a-hash"),
        ShareholdingQuarantine(
            isin=None, xbrl_element="x", context_ref=None, raw_value=None, reason=Reason.UNMAPPED_ELEMENT,
            detail="d", **_prov(),
        ),
    ],
    ids=["filing-on-as-on-date", "isin-format", "negative-count", "model-version", "negative-value",
         "pattern-on-as-on-date", "content-hash", "half-fact"],
)  # fmt: skip
def test_db_constraints(session, row):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(row)
            session.flush()


def test_stores_are_append_only(session, tmp_path):
    _drop(tmp_path, VEDL_FILE, VEDL_PUBLISHED)
    NseShpDrop().run(_ctx(session, tmp_path, VEDL_PUBLISHED + timedelta(days=1)))
    for model in (ShareholdingFiling, ShareholdingPattern):
        row = session.scalars(select(model).limit(1)).one()
        with pytest.raises(Exception, match="append-only"):
            with session.begin_nested():
                session.delete(row)
                session.flush()
