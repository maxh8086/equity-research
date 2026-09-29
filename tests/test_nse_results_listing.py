"""Live NSE results-listing adapter (Session 4): listing JSON and XBRL fixtures over a mock transport.

No test makes a network call. The XBRL bytes are the recorded filings in
tests/fixtures/nse_xbrl; the listing is tests/fixtures/nse_results_listing.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    FinancialFact,
    FinancialFactsQuarantine,
    FinancialFiling,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    RawSourceFile,
    XbrlIsinBasis,
)
from core.db.models import FinancialFactsQuarantineReason as Reason
from core.db.pit import raw_source_file_by_url_as_of
from core.sources import DeploymentMode, SourceClass
from core.timezones import IST
from ingest import registry
from ingest.base import AdapterContext, CanaryStatus, RunStatus
from ingest.nse_results_listing.adapters import NseResultsListing
from ingest.nse_results_listing.schema import ResultsListingRow
from ingest.nse_xbrl.parser import parse_filing
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

ROOT = Path(__file__).parent / "fixtures"
XBRL = ROOT / "nse_xbrl"
LISTING = (ROOT / "nse_results_listing" / "results_listing_infy_quarterly.json").read_bytes()
ARCHIVE = "https://nsearchives.nseindia.com/corporate/xbrl/"
NAME = "nse_results_listing"
INFY = "INE009A01021"
Q2 = "INDAS_112850_1276017_17102024074402.xml"
Q2_PUBLISHED = datetime(2024, 10, 17, 19, 44, 31, tzinfo=IST)
FY = "INDAS_104589_1099938_19042024112830.xml"
FY_PUBLISHED = datetime(2024, 4, 19, 11, 32, 6, tzinfo=IST)
NOW = Q2_PUBLISHED + timedelta(days=1)


class Nse:
    """Mock NSE: homepage, results listing per symbol, XBRL archive. Records every request."""

    def __init__(self, listing: bytes = LISTING, xbrl: dict[str, bytes] | None = None, block: str | None = None):
        self.listing = listing
        self.xbrl = xbrl if xbrl is not None else {p.name: p.read_bytes() for p in XBRL.glob("*.xml")}
        self.block = block  # a path fragment answered with HTTP 429
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if self.block and self.block in url:
            return httpx.Response(429)
        if request.url.path == "/api/corporates-financial-results":
            return httpx.Response(200, content=self.listing, headers={"content-type": "application/json"})
        if url.startswith(ARCHIVE):
            name = url.removeprefix(ARCHIVE)
            if name in self.xbrl:
                return httpx.Response(200, content=self.xbrl[name], headers={"content-type": "application/xml"})
            return httpx.Response(404)
        if request.url.host == "www.nseindia.com" and request.url.path == "/":
            return httpx.Response(200, text="home")
        return httpx.Response(404)

    def adapter(self, **kw) -> NseResultsListing:
        return NseResultsListing(transport=httpx.MockTransport(self), **kw)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def _ctx(session, now=NOW, blob=None, **overrides) -> AdapterContext:
    settings = Settings(
        web_scraping_enabled=True, source_switches={NAME: True}, nse_min_interval_seconds=0.0, **overrides
    )
    return AdapterContext(session, blob or MemoryBlobStore(), settings, now=lambda: now)


def _index_list(session, symbol: str, isin: str, published: datetime) -> None:
    prov = dict(as_of=published, content_hash=content_hash(f"{symbol}{published}".encode()),
                source_url=f"list-{published}", extracted_by="tests", model_version=None)  # fmt: skip
    snap = IndexSnapshot(index_code=IndexCode.NIFTY_50, constituent_count=1, quarantined_rows=0,
                         rule_version="t", **prov)  # fmt: skip
    session.add(snap)
    session.flush()
    session.add(
        IndexSnapshotConstituent(snapshot_id=snap.id, isin=isin, symbol=symbol, series="EQ", company_name="C",
                                 industry="I", row_number=1, **prov)  # fmt: skip
    )
    session.flush()


@pytest.fixture
def universe(session):
    _index_list(session, "INFY", INFY, datetime(2024, 4, 15, 9, 0, tzinfo=IST))


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _xbrl_raw(session) -> list[RawSourceFile]:
    r = RawSourceFile
    return list(session.scalars(select(r).where(r.extracted_by == NseResultsListing.extracted_by(),
                                                r.media_type.like("%xml%")).order_by(r.id)))  # fmt: skip


# --------------------------------------------------------------------------- #
# Declaration, switches
# --------------------------------------------------------------------------- #


def test_declares_itself_as_a_web_scrape_source():
    assert NseResultsListing.name == NAME
    assert NseResultsListing.source_class is SourceClass.WEB_SCRAPE
    assert set(NseResultsListing.target_stores) == {"financial_facts", "financial_filing", "financial_facts_quarantine"}
    assert NseResultsListing.switch_name() == "EQUITY_SOURCE_NSE_RESULTS_LISTING_ENABLED"
    assert registry.discover()[NAME] is NseResultsListing


def test_default_off_and_writes_nothing(session):
    nse = Nse()
    result = nse.adapter().run(AdapterContext(session, MemoryBlobStore(), Settings(), now=lambda: NOW))
    assert result.status is RunStatus.DISABLED and nse.requests == []
    off = _ctx(session)
    off.settings.source_switches[NAME] = False
    assert nse.adapter().run(off).status is RunStatus.DISABLED and nse.requests == []
    assert _count(session, FinancialFiling) == 0


def test_commercial_mode_refuses_to_start_with_the_switch_on():
    adapters = registry.discover()
    settings = Settings(deployment_mode=DeploymentMode.COMMERCIAL, source_switches={NAME: True})
    with pytest.raises(registry.StartupRefused, match=NAME):
        registry.check_startup(settings, adapters)


def test_config_carries_the_listing_endpoint():
    s = Settings()
    assert s.nse_results_listing_api_url == "https://www.nseindia.com/api/corporates-financial-results"
    assert s.nse_results_listing_periods == ["Quarterly"]


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_row_reads_only_the_fields_it_needs_and_parses_dissemination_time_as_ist():
    (row, *_) = [ResultsListingRow.model_validate(r) for r in json.loads(LISTING)]
    assert row.symbol == "INFY" and row.xbrl == ARCHIVE + Q2
    assert row.exchdisstime == Q2_PUBLISHED and row.exchdisstime.utcoffset() == timedelta(hours=5, minutes=30)


def test_row_missing_a_needed_field_fails_loudly():
    with pytest.raises(ValueError):
        ResultsListingRow.model_validate({"symbol": "INFY", "exchdisstime": "17-Oct-2024 19:44:31"})
    with pytest.raises(ValueError):
        ResultsListingRow.model_validate({"symbol": "INFY", "xbrl": "-", "exchdisstime": "2024-10-17"})


# --------------------------------------------------------------------------- #
# Loading: same facts and quarantine as a hand-dropped file
# --------------------------------------------------------------------------- #


def test_fetches_listing_for_universe_symbol_and_period(session, universe):
    nse = Nse()
    nse.adapter().run(_ctx(session))
    (listing_req,) = [r for r in nse.requests if r.url.path == "/api/corporates-financial-results"]
    assert dict(listing_req.url.params) == {"index": "equities", "symbol": "INFY", "period": "Quarterly"}
    assert "equity-knowledge" in listing_req.headers["user-agent"]


def test_loads_filing_facts_and_quarantine_like_the_drop_adapter(session, universe):
    result = Nse().adapter().run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail

    q2 = session.scalars(select(FinancialFiling).where(FinancialFiling.content_hash == content_hash(
        (XBRL / Q2).read_bytes()))).one()  # fmt: skip
    assert (q2.isin, q2.isin_basis, q2.symbol) == (INFY, XbrlIsinBasis.INDEX_LIST, "INFY")
    assert q2.as_of == Q2_PUBLISHED and q2.source_url == ARCHIVE + Q2 and q2.model_version is None
    assert q2.extracted_by == NseResultsListing.extracted_by()
    assert (q2.facts_written, q2.facts_quarantined) == (225, 2)

    expected_fy = parse_filing((XBRL / FY).read_bytes())
    fy = session.scalars(select(FinancialFiling).where(FinancialFiling.as_of == FY_PUBLISHED)).one()
    assert fy.facts_written == len(expected_fy.facts) and fy.period_end == date(2024, 3, 31)

    assert result.rows_written == 225 + len(expected_fy.facts)
    assert _count(session, FinancialFact) == result.rows_written
    assert {q.reason for q in session.scalars(select(FinancialFactsQuarantine))} == {Reason.CONFLICTING_VALUES}


def test_stores_each_xbrl_file_raw_with_provenance_before_parsing(session, universe):
    blob = MemoryBlobStore()
    Nse().adapter().run(_ctx(session, blob=blob))
    raws = {r.source_url: r for r in _xbrl_raw(session)}
    assert set(raws) == {ARCHIVE + Q2, ARCHIVE + FY}
    q2 = raws[ARCHIVE + Q2]
    data = (XBRL / Q2).read_bytes()
    assert q2.content_hash == content_hash(data) and blob.get(q2.content_hash) == data
    assert q2.as_of == Q2_PUBLISHED and q2.fetched_at == NOW and q2.byte_size == len(data)


def test_filing_without_an_xbrl_attachment_is_skipped_not_downloaded(session, universe):
    nse = Nse()
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED
    assert all("-" != p.rsplit("/", 1)[-1] for p in nse.paths())
    assert sorted(p.rsplit("/", 1)[-1] for p in nse.paths() if p.startswith("/corporate/xbrl/")) == sorted([Q2, FY])


def test_pre_2020_taxonomy_is_quarantined_not_parsed(session, universe):
    old = (XBRL / Q2).read_bytes().replace(b"Ind-AS_entry_point_2020-03-31.xsd", b"Ind-AS_entry_point_2016-03-31.xsd")
    nse = Nse(xbrl={Q2: old, FY: (XBRL / FY).read_bytes()})
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED  # a rejected file needs a person
    reasons = [q.reason for q in session.scalars(select(FinancialFactsQuarantine).where(
        FinancialFactsQuarantine.xbrl_element.is_(None)))]  # fmt: skip
    assert reasons == [Reason.UNSUPPORTED_TAXONOMY]
    assert _count(session, FinancialFiling) == 1  # only the yearly file loaded


def test_unresolvable_isin_is_quarantined_then_loads_once_a_list_exists(session):
    blob = MemoryBlobStore()
    first = Nse().adapter(symbols=["INFY"]).run(_ctx(session, blob=blob))
    assert first.status is RunStatus.FAILED and first.rows_written == 0
    quarantined = list(session.scalars(select(FinancialFactsQuarantine)))
    assert {q.reason for q in quarantined} == {Reason.ISIN_UNRESOLVED} and len(quarantined) == 2
    assert _count(session, FinancialFiling) == 0

    _index_list(session, "INFY", INFY, datetime(2024, 4, 15, 9, 0, tzinfo=IST))
    second = Nse().adapter(symbols=["INFY"]).run(_ctx(session, now=NOW + timedelta(days=1), blob=blob))
    assert second.status is RunStatus.SUCCEEDED and second.rows_written > 0
    assert _count(session, FinancialFiling) == 2
    assert len(_xbrl_raw(session)) == 2  # the retry re-read stored bytes, no second raw row


# --------------------------------------------------------------------------- #
# Idempotence
# --------------------------------------------------------------------------- #


def test_rerun_adds_no_facts_no_raw_rows_and_downloads_nothing(session, universe):
    blob = MemoryBlobStore()
    first = Nse().adapter().run(_ctx(session, blob=blob))
    counts = (_count(session, FinancialFact), _count(session, FinancialFiling), _count(session, FinancialFactsQuarantine),
              len(_xbrl_raw(session)))  # fmt: skip

    nse = Nse()
    second = nse.adapter().run(_ctx(session, now=NOW + timedelta(hours=6), blob=blob))
    assert second.status is RunStatus.SUCCEEDED, second.detail
    assert second.rows_written == 0 and second.quarantined == 0
    assert (_count(session, FinancialFact), _count(session, FinancialFiling), _count(session, FinancialFactsQuarantine),
            len(_xbrl_raw(session))) == counts  # fmt: skip
    assert not [p for p in nse.paths() if p.startswith("/corporate/xbrl/")]
    assert first.rows_written > 0


def test_pit_reader_finds_a_stored_file_by_url_and_publication(session, universe):
    Nse().adapter().run(_ctx(session))
    args = dict(source_url=ARCHIVE + Q2, file_as_of=Q2_PUBLISHED, extracted_by=NseResultsListing.extracted_by())
    assert raw_source_file_by_url_as_of(session, as_of=NOW, **args).content_hash == content_hash((XBRL / Q2).read_bytes())
    assert raw_source_file_by_url_as_of(session, as_of=Q2_PUBLISHED - timedelta(seconds=1), **args) is None
    assert raw_source_file_by_url_as_of(session, as_of=NOW, **{**args, "source_url": ARCHIVE + "other.xml"}) is None


def test_reparse_rereads_stored_bytes_without_the_network(session, universe):
    blob = MemoryBlobStore()
    adapter = Nse().adapter()
    adapter.run(_ctx(session, blob=blob))
    nse = Nse()
    result = nse.adapter().reparse(_ctx(session, now=NOW + timedelta(days=1), blob=blob))
    assert result.status is RunStatus.SUCCEEDED and result.rows_written == 0
    assert nse.requests == []


# --------------------------------------------------------------------------- #
# Blocks, drift, bad data
# --------------------------------------------------------------------------- #


def test_block_on_the_listing_stops_the_whole_run(session, universe):
    _index_list(session, "TCS", "INE467B01029", datetime(2024, 4, 16, 9, 0, tzinfo=IST))
    nse = Nse(block="/api/corporates-financial-results")
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED and "blocked" in result.detail.lower()
    assert [p for p in nse.paths() if p == "/api/corporates-financial-results"] == ["/api/corporates-financial-results"]
    assert _count(session, FinancialFiling) == 0


def test_block_on_the_homepage_stops_before_any_api_call(session, universe):
    nse = Nse(block="https://www.nseindia.com")
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED and "blocked" in result.detail.lower()
    assert len(nse.requests) == 1


def test_block_on_an_xbrl_download_stops_the_run_and_keeps_earlier_work(session, universe):
    nse = Nse(block=ARCHIVE + FY)
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED and "blocked" in result.detail.lower()
    assert _count(session, FinancialFiling) == 1  # Q2 (listed first) had already loaded


def test_listing_that_drops_a_needed_field_fails_the_run_loudly(session, universe):
    bad = json.dumps([{"symbol": "INFY", "exchdisstime": "17-Oct-2024 19:44:31"}]).encode()
    result = Nse(listing=bad).adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED and "INFY" in result.detail
    assert _count(session, FinancialFiling) == 0


def test_listing_that_is_not_a_list_fails_the_run(session, universe):
    result = Nse(listing=b'{"data": []}').adapter().run(_ctx(session))
    assert result.status is RunStatus.FAILED


def test_dissemination_time_in_the_future_is_reported_not_stored(session, universe):
    result = Nse().adapter().run(_ctx(session, now=Q2_PUBLISHED - timedelta(hours=1)))
    assert result.status is RunStatus.FAILED and "after" in result.detail
    assert not [r for r in _xbrl_raw(session) if r.source_url == ARCHIVE + Q2]


def test_empty_universe_makes_no_requests(session):
    nse = Nse()
    result = nse.adapter().run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED and nse.requests == []


def test_configured_periods_are_each_requested(session, universe):
    nse = Nse(listing=b"[]")
    nse.adapter().run(_ctx(session, nse_results_listing_periods=["Quarterly", "Annual"]))
    periods = [r.url.params["period"] for r in nse.requests if r.url.path == "/api/corporates-financial-results"]
    assert periods == ["Quarterly", "Annual"]


def test_canary_validates_a_live_sample_and_raises_on_drift(session, universe):
    ok = Nse().adapter().canary(_ctx(session))
    assert ok.status is CanaryStatus.PASSED
    with pytest.raises(ValueError):
        Nse(listing=b'[{"symbol": "INFY"}]').adapter().canary(_ctx(session))
