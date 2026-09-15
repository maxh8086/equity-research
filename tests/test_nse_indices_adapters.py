"""NSE Indices constituent adapters against recorded responses (tests/fixtures/nse_indices)."""

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.compute.hashing import content_hash
from core.compute.membership import Membership
from core.config import Settings
from core.db.models import (
    Entity,
    EntityIsin,
    EntityLinkBasis,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotQuarantine,
    QuarantineReason,
    RawSourceFile,
)
from core.db.pit import index_members_on, index_snapshot_quarantine_as_of, index_snapshots_as_of
from core.timezones import IST
from ingest.base import AdapterContext, CanaryStatus, RunStatus
from ingest.nse_indices import adapters
from ingest.nse_indices.adapters import (
    NseIndicesConstituents,
    NseIndicesConstituentsDrop,
    WaybackIndexConstituents,
    wayback_digest,
)
from ingest.nse_indices.parser import ListRejected, parse_constituent_list
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures" / "nse_indices"
NIFTY_50_NOW = (FIXTURES / "ind_nifty50list_20260915.csv").read_bytes()
NEXT_50_NOW = (FIXTURES / "ind_niftynext50list_20260915.csv").read_bytes()
NIFTY_50_2016 = (FIXTURES / "ind_nifty50list_20160310050302.csv").read_bytes()
NIFTY_50_2017 = (FIXTURES / "ind_nifty50list_20171016083142.csv").read_bytes()
CDX = [row[:5] for row in json.loads((FIXTURES / "cdx_niftyindices_ind_nifty50list.json").read_bytes())]

FETCHED = datetime(2026, 9, 15, 14, 0, tzinfo=IST)
ARCHIVE = "https://archives.nseindia.com/content/indices"
N50, NN50 = IndexCode.NIFTY_50, IndexCode.NIFTY_NEXT_50

# The recorded CDX response opens with five captures of identical bytes.
RUN_2017 = CDX[:6]
FIRST, LAST = CDX[1], CDX[5]
FIRST_AT = datetime(2017, 10, 16, 8, 31, 42, tzinfo=timezone.utc)
LAST_AT = datetime(2018, 3, 29, 15, 27, 22, tzinfo=timezone.utc)


def _isins(data: bytes, index_code: IndexCode) -> frozenset[str]:
    return frozenset(c.row.isin for c in parse_constituent_list(data, index_code).constituents)


def capture_url(row: list[str]) -> str:
    return f"https://web.archive.org/web/{row[0]}id_/{row[1]}"


def page(content: bytes = b"", status: int = 200, headers: dict | None = None) -> tuple:
    return status, content, headers or {"content-type": "text/csv"}


class Web:
    """httpx.MockTransport handler: exact URLs, and CDX answers keyed by the queried list URL."""

    def __init__(self, pages: dict[str, tuple] | None = None, cdx: dict[str, list] | None = None) -> None:
        self.pages = {str(httpx.URL(url)): p for url, p in (pages or {}).items()}
        self.cdx = cdx or {}
        self.requests: list[httpx.Request] = []

    def set(self, url: str, p: tuple) -> None:
        self.pages[str(httpx.URL(url))] = p

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/cdx/search/cdx":
            return httpx.Response(200, content=json.dumps(self.cdx.get(request.url.params["url"], [])).encode())
        status, content, headers = self.pages.get(str(request.url), page(status=404))
        # stream=, not content=: a network response arrives unread, so get_with_raw can read it raw.
        return httpx.Response(status, stream=httpx.ByteStream(content), headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def captures_fetched(self) -> list[str]:
        return [str(r.url) for r in self.requests if "id_/" in str(r.url)]


def _ctx(session, now: datetime = FETCHED, **settings) -> AdapterContext:
    base = dict(
        web_scraping_enabled=True,
        nse_indices_min_interval_seconds=0,
        wayback_min_interval_seconds=0,
        source_switches={
            "nse_indices_constituents": True,
            "nse_indices_constituents_drop": True,
            "wayback_nse_indices_constituents": True,
        },
    )
    return AdapterContext(session, MemoryBlobStore(), Settings(**(base | settings)), now=lambda: now)


def _live(nifty: bytes = NIFTY_50_NOW, next50: bytes = NEXT_50_NOW) -> Web:
    return Web({f"{ARCHIVE}/ind_nifty50list.csv": page(nifty), f"{ARCHIVE}/ind_niftynext50list.csv": page(next50)})


def _wayback(first: bytes = NIFTY_50_2017, last: bytes = NIFTY_50_2017) -> Web:
    return Web(
        {capture_url(FIRST): page(first), capture_url(LAST): page(last)},
        cdx={"niftyindices.com/IndexConstituent/ind_nifty50list.csv": RUN_2017},
    )


def _all(session, model) -> list:
    return list(session.scalars(select(model)))


# --------------------------------------------------------------------------- #
# Live archive lists
# --------------------------------------------------------------------------- #


def test_live_lists_become_snapshots_members_and_entities(session):
    web, ctx = _live(), _ctx(session)
    result = NseIndicesConstituents(web.transport()).run(ctx)
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.raw_files, result.rows_written, result.quarantined) == (2, 100, 0)
    assert {r.headers["User-Agent"] for r in web.requests} == {ctx.settings.http_user_agent}

    assert {r.as_of for r in _all(session, RawSourceFile)} == {FETCHED}
    assert index_members_on(session, index_code=NN50, on=FETCHED, as_of=FETCHED) == Membership(
        _isins(NEXT_50_NOW, NN50), frozenset()
    )
    # Nothing is visible before the lists were fetched (R2).
    assert index_members_on(
        session, index_code=N50, on=FETCHED, as_of=FETCHED - timedelta(microseconds=1)
    ) == Membership(frozenset(), frozenset())

    links = _all(session, EntityIsin)
    assert len(links) == len(_all(session, Entity)) == 100
    assert {link.basis for link in links} == {EntityLinkBasis.FIRST_SEEN}


def test_changed_header_quarantines_the_file_keeps_its_bytes_and_fails(session):
    changed = NEXT_50_NOW.replace(b"ISIN Code", b"ISIN", 1)
    result = NseIndicesConstituents(_live(next50=changed).transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    [q] = index_snapshot_quarantine_as_of(session, as_of=FETCHED)
    assert (q.reason, q.index_code, q.snapshot_id, q.content_hash) == (
        QuarantineReason.SHAPE_CHANGED, NN50, None, content_hash(changed)
    )  # fmt: skip
    assert len(_all(session, RawSourceFile)) == 2
    assert [s.index_code for s in _all(session, IndexSnapshot)] == [N50]


def test_bad_row_is_quarantined_and_the_snapshot_is_incomplete(session):
    isin = sorted(_isins(NIFTY_50_NOW, N50))[0]
    wrong = isin[:-1] + str((int(isin[-1]) + 1) % 10)
    NseIndicesConstituents(_live(nifty=NIFTY_50_NOW.replace(isin.encode(), wrong.encode())).transport()).run(
        _ctx(session)
    )
    [snapshot] = index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)
    assert not snapshot.complete and len(snapshot.isins) == 49
    [q] = index_snapshot_quarantine_as_of(session, as_of=FETCHED)
    assert q.reason is QuarantineReason.INVALID_ISIN and q.snapshot_id is not None and wrong in q.raw_row


def test_blocked_source_stops_without_retrying_and_stores_nothing(session):
    web = Web({f"{ARCHIVE}/ind_nifty50list.csv": page(status=403)})
    result = NseIndicesConstituents(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "403" in result.detail
    assert [r.url.path for r in web.requests] == ["/robots.txt", "/content/indices/ind_nifty50list.csv"]
    assert _all(session, RawSourceFile) == []


def test_canary_checks_live_shape_without_storing(session):
    ctx = _ctx(session)
    assert NseIndicesConstituents(_live().transport()).canary(ctx).status is CanaryStatus.PASSED
    assert _all(session, RawSourceFile) == []
    with pytest.raises(ListRejected):
        NseIndicesConstituents(_live(next50=b"Symbol,ISIN\n").transport()).canary(ctx)


# --------------------------------------------------------------------------- #
# Wayback captures
# --------------------------------------------------------------------------- #


def test_recorded_run_is_five_identical_captures():
    assert {row[4] for row in RUN_2017[1:]} == {FIRST[4]} != {CDX[6][4]}
    assert (FIRST[0], LAST[0]) == ("20171016083142", "20180329152722")


def test_wayback_loads_run_boundaries_dated_by_capture_time(session):
    web = _wayback()
    result = WaybackIndexConstituents(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert web.captures_fetched() == [str(httpx.URL(capture_url(FIRST))), str(httpx.URL(capture_url(LAST)))]
    assert [s.observed_at for s in index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)] == [FIRST_AT, LAST_AT]

    listed_2017 = _isins(NIFTY_50_2017, N50)
    new_year_2018 = datetime(2018, 1, 1, tzinfo=IST)
    assert index_members_on(session, index_code=N50, on=new_year_2018, as_of=FETCHED) == Membership(
        listed_2017, frozenset()
    )
    # Before the March 2018 capture existed, 1 Jan 2018 lay after the last known list (R2).
    assert index_members_on(
        session, index_code=N50, on=new_year_2018, as_of=LAST_AT - timedelta(seconds=1)
    ) == Membership(frozenset(), listed_2017)


def test_wayback_rerun_skips_captures_already_stored(session):
    WaybackIndexConstituents(_wayback().transport()).run(_ctx(session))
    web = _wayback()
    result = WaybackIndexConstituents(web.transport()).run(_ctx(session, now=FETCHED + timedelta(hours=1)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert web.captures_fetched() == []
    assert len(index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)) == 2


def test_capture_that_differs_from_its_digest_is_quarantined(session):
    result = WaybackIndexConstituents(_wayback(last=NIFTY_50_2016).transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED
    [q] = index_snapshot_quarantine_as_of(session, as_of=FETCHED)
    assert (q.reason, q.source_url, q.as_of) == (QuarantineReason.DIGEST_MISMATCH, capture_url(LAST), LAST_AT)
    assert len(index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)) == 1


def test_archive_overload_is_retried_a_bounded_number_of_times_then_stops_keeping_what_loaded(session):
    web = _wayback()
    web.set(capture_url(LAST), page(b"Temporarily Offline", status=503))
    ctx = _ctx(session, wayback_overload_retries=2, wayback_retry_backoff_seconds=0)
    result = WaybackIndexConstituents(web.transport()).run(ctx)
    assert result.status is RunStatus.FAILED and "503" in result.detail
    assert len(web.captures_fetched()) == 1 + 3  # first capture, then the last one tried three times
    assert [s.observed_at for s in index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)] == [FIRST_AT]


def test_gzip_archived_capture_is_checked_as_transferred_and_stored_decoded(session):
    gz = gzip.compress(NIFTY_50_2017, mtime=0)
    cdx = [CDX[0], [*FIRST[:4], wayback_digest(gz)]]
    web = Web(
        {capture_url(FIRST): page(gz, headers={"content-type": "text/csv", "content-encoding": "gzip"})},
        cdx={"niftyindices.com/IndexConstituent/ind_nifty50list.csv": cdx},
    )
    result = WaybackIndexConstituents(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    [snapshot] = index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)
    assert snapshot.isins == _isins(NIFTY_50_2017, N50)
    assert content_hash(NIFTY_50_2017) in {r.content_hash for r in _all(session, RawSourceFile)}


def test_whole_file_quarantine_is_examined_again_only_after_a_rule_change(session, monkeypatch):
    WaybackIndexConstituents(_wayback(last=NIFTY_50_2016).transport()).run(_ctx(session))
    same_rule = _wayback()
    WaybackIndexConstituents(same_rule.transport()).run(_ctx(session, now=FETCHED + timedelta(hours=1)))
    assert same_rule.captures_fetched() == []

    monkeypatch.setattr(adapters, "RULE_VERSION", "nse_indices_constituents/next")
    new_rule = _wayback()
    result = WaybackIndexConstituents(new_rule.transport()).run(_ctx(session, now=FETCHED + timedelta(hours=2)))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert new_rule.captures_fetched() == [str(httpx.URL(capture_url(LAST)))]
    assert len(index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)) == 2


def test_redirect_to_another_capture_is_not_stored_under_the_requested_time(session):
    web = _wayback()
    web.set(capture_url(LAST), page(status=302, headers={"location": capture_url(FIRST)}))
    result = WaybackIndexConstituents(web.transport()).run(_ctx(session))
    assert result.status is RunStatus.FAILED and "redirected" in result.detail
    assert [s.observed_at for s in index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)] == [FIRST_AT]
    assert LAST_AT not in {r.as_of for r in _all(session, RawSourceFile)}


def test_earlier_evidence_loaded_later_appends_an_earlier_link_to_the_same_entity(session):
    NseIndicesConstituents(_live().transport()).run(_ctx(session))
    WaybackIndexConstituents(_wayback().transport()).run(_ctx(session, now=FETCHED + timedelta(hours=1)))
    isin = sorted(_isins(NIFTY_50_NOW, N50) & _isins(NIFTY_50_2017, N50))[0]
    links = list(session.scalars(select(EntityIsin).where(EntityIsin.isin == isin).order_by(EntityIsin.as_of)))
    assert [link.as_of for link in links] == [FIRST_AT, FETCHED]
    assert len({link.entity_id for link in links}) == 1


# --------------------------------------------------------------------------- #
# Drop folder
# --------------------------------------------------------------------------- #


def test_dropped_capture_is_dated_by_its_sidecar_and_loaded_once(session, tmp_path):
    folder = tmp_path / NseIndicesConstituentsDrop.name
    folder.mkdir()
    (folder / "nifty50_2016.csv").write_bytes(NIFTY_50_2016)
    url = "https://web.archive.org/web/20160310050302id_/http://www.nseindia.com/content/indices/ind_nifty50list.csv"
    meta = {"source_url": url, "published_at": "2016-03-10T05:03:02+00:00", "media_type": "application/csv"}
    (folder / "nifty50_2016.csv.meta.json").write_text(json.dumps(meta))

    assert NseIndicesConstituentsDrop().run(_ctx(session, drop_folder=tmp_path)).status is RunStatus.SUCCEEDED
    rerun = NseIndicesConstituentsDrop().run(_ctx(session, now=FETCHED + timedelta(hours=1), drop_folder=tmp_path))
    assert rerun.status is RunStatus.SUCCEEDED and "1 already loaded" in rerun.detail

    [snapshot] = index_snapshots_as_of(session, index_code=N50, as_of=FETCHED)
    assert snapshot.observed_at == datetime(2016, 3, 10, 5, 3, 2, tzinfo=timezone.utc)
    assert snapshot.isins == _isins(NIFTY_50_2016, N50)


def test_dropped_file_for_an_unknown_list_is_quarantined(session, tmp_path):
    folder = tmp_path / NseIndicesConstituentsDrop.name
    folder.mkdir()
    (folder / "n100.csv").write_bytes(NIFTY_50_NOW)
    meta = {"source_url": f"{ARCHIVE}/ind_nifty100list.csv", "published_at": "2026-09-12T09:00:25+05:30", "media_type": "text/csv"}
    (folder / "n100.csv.meta.json").write_text(json.dumps(meta))
    assert NseIndicesConstituentsDrop().run(_ctx(session, drop_folder=tmp_path)).status is RunStatus.FAILED
    [q] = index_snapshot_quarantine_as_of(session, as_of=FETCHED)
    assert (q.reason, q.index_code) == (QuarantineReason.UNKNOWN_FILE, None)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

PROVENANCE = dict(
    content_hash=content_hash(b"list"), source_url=f"{ARCHIVE}/ind_nifty50list.csv", extracted_by="tests", model_version=None
)


def _link(session, isin: str, as_of: datetime) -> None:
    entity = Entity(as_of=as_of, **PROVENANCE)
    session.add(entity)
    session.flush()
    session.add(EntityIsin(entity_id=entity.id, isin=isin, basis=EntityLinkBasis.FIRST_SEEN, as_of=as_of, **PROVENANCE))
    session.flush()


def test_one_isin_cannot_belong_to_two_entities(session):
    _link(session, "INE002A01018", FETCHED)
    with pytest.raises(DBAPIError, match="already linked"):
        with session.begin_nested():
            _link(session, "INE002A01018", FETCHED + timedelta(days=1))


@pytest.mark.parametrize(
    "row",
    [
        IndexSnapshotQuarantine(row_number=3, reason=QuarantineReason.MALFORMED_ROW, detail="x", rule_version="t", as_of=FETCHED, **PROVENANCE),
        IndexSnapshot(index_code=N50, constituent_count=50, quarantined_rows=0, rule_version="t", as_of=FETCHED, **(PROVENANCE | {"model_version": "claude-sonnet-5"})),
        IndexSnapshot(index_code=N50, constituent_count=-1, quarantined_rows=0, rule_version="t", as_of=FETCHED, **PROVENANCE),
    ],
    ids=["row-quarantine-without-snapshot", "model-version", "negative-count"],
)  # fmt: skip
def test_db_constraints(session, row):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(row)
            session.flush()


@pytest.mark.parametrize(
    "table", ["entity", "entity_isin", "index_snapshot", "index_snapshot_constituent", "index_snapshot_quarantine"]
)
@pytest.mark.parametrize("sql", ["UPDATE {t} SET as_of = as_of", "DELETE FROM {t}", "TRUNCATE {t} CASCADE"])
def test_append_only(session, table, sql):
    NseIndicesConstituents(_live(next50=b"not a list").transport()).run(_ctx(session))
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text(sql.format(t=table)))
