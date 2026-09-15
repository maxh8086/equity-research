from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import RawSourceFile
from core.db.pit import raw_source_files_as_of
from core.sources import SourceClass
from core.timezones import IST
from ingest.base import AdapterContext, DropFileError, DropFolderAdapter, RunResult, RunStatus
from tests.fakes import FailingBlobStore, MemoryBlobStore

pytestmark = pytest.mark.db

PUBLISHED = datetime(2024, 3, 1, 23, 59, 59, tzinfo=IST)
FETCHED = datetime(2026, 9, 15, 10, 0, tzinfo=IST)
URL = "https://www.niftyindices.com/example.csv"


class Drop(DropFolderAdapter):
    name = "nse_indices_drop"
    target_stores = ("index_membership",)

    def ingest(self, ctx):
        docs = list(self.drop_files(ctx))
        return RunResult(self.name, RunStatus.SUCCEEDED, raw_files=len(docs))


def _ctx(session, blob=None, **settings) -> AdapterContext:
    return AdapterContext(session, blob or MemoryBlobStore(), Settings(**settings), now=lambda: FETCHED)


def _stored(session) -> list[RawSourceFile]:
    return list(session.scalars(select(RawSourceFile)))


def _row(**overrides) -> RawSourceFile:
    fields = dict(
        fetched_at=FETCHED,
        byte_size=3,
        media_type="text/csv",
        as_of=PUBLISHED,
        content_hash=content_hash(b"csv"),
        source_url=URL,
        extracted_by="tests",
        model_version=None,
    )
    return RawSourceFile(**(fields | overrides))


def _assert_rejected(session, row, exc=IntegrityError):
    with pytest.raises(exc):
        with session.begin_nested():
            session.add(row)
            session.flush()


# --------------------------------------------------------------------------- #
# store_raw
# --------------------------------------------------------------------------- #


def test_store_raw_saves_blob_then_records_provenance(session):
    blob = MemoryBlobStore()
    doc = Drop().store_raw(
        _ctx(session, blob), data=b"a,b\n1,2\n", source_url=URL, as_of=PUBLISHED, media_type="text/csv"
    )
    assert blob.get(doc.content_hash) == b"a,b\n1,2\n"
    [row] = _stored(session)
    assert (row.content_hash, row.source_url, row.as_of, row.fetched_at) == (
        doc.content_hash, URL, PUBLISHED, FETCHED
    )  # fmt: skip
    assert row.extracted_by == "tests.test_raw_source_file.Drop"
    assert row.byte_size == 8


def test_no_provenance_row_when_blob_write_fails(session):
    with pytest.raises(ConnectionError):
        Drop().store_raw(
            _ctx(session, FailingBlobStore()), data=b"x", source_url=URL, as_of=PUBLISHED, media_type="text/csv"
        )
    assert _stored(session) == []


def test_publication_after_fetch_rejected_before_anything_is_stored(session):
    blob = MemoryBlobStore()
    with pytest.raises(ValueError, match="after fetch time"):
        Drop().store_raw(
            _ctx(session, blob), data=b"x", source_url=URL,
            as_of=FETCHED + timedelta(seconds=1), media_type="text/csv",
        )  # fmt: skip
    assert blob.objects == {} and _stored(session) == []


def test_naive_as_of_rejected(session):
    with pytest.raises(ValueError, match="timezone-aware"):
        Drop().store_raw(
            _ctx(session), data=b"x", source_url=URL, as_of=datetime(2024, 3, 1), media_type="text/csv"
        )


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_db_rejects_as_of_after_fetched_at(session):
    _assert_rejected(session, _row(as_of=FETCHED + timedelta(microseconds=1)))


def test_db_evaluates_as_of_against_fetch_across_time_zones(session):
    session.add(_row(as_of=FETCHED.astimezone(timezone.utc)))
    session.flush()


@pytest.mark.parametrize(
    "overrides",
    [{"model_version": "claude-sonnet-5"}, {"content_hash": "nope"}, {"byte_size": -1}],
)
def test_db_constraints(session, overrides):
    _assert_rejected(session, _row(**overrides))


def test_same_fetch_recorded_once(session):
    session.add(_row())
    session.flush()
    _assert_rejected(session, _row())


def test_naive_fetched_at_rejected_before_reaching_db():
    with pytest.raises(ValueError, match="timezone-aware"):
        _row(fetched_at=datetime(2026, 9, 15))


@pytest.mark.parametrize(
    "sql",
    ["UPDATE raw_source_file SET byte_size = 0", "DELETE FROM raw_source_file", "TRUNCATE raw_source_file"],
)
def test_append_only(session, sql):
    session.add(_row())
    session.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        with session.begin_nested():
            session.execute(text(sql))


def test_pit_read_filters_by_publication_time_and_adapter(session):
    early = datetime(2020, 1, 1, tzinfo=IST)
    session.add_all(
        [
            _row(as_of=PUBLISHED, content_hash=content_hash(b"late")),
            _row(as_of=early, content_hash=content_hash(b"early")),
            _row(as_of=early, content_hash=content_hash(b"other"), extracted_by="other"),
        ]
    )
    session.flush()
    assert [r.as_of for r in raw_source_files_as_of(session, extracted_by="tests", as_of=FETCHED)] == [
        early, PUBLISHED
    ]  # fmt: skip
    [only] = raw_source_files_as_of(session, extracted_by="tests", as_of=PUBLISHED - timedelta(seconds=1))
    assert only.as_of == early


# --------------------------------------------------------------------------- #
# Drop folder
# --------------------------------------------------------------------------- #

META = '{"source_url": "%s", "published_at": "2024-03-01T23:59:59+05:30", "media_type": "text/csv"}' % URL


def _drop(tmp_path, files: dict[str, str]):
    folder = tmp_path / Drop.name
    folder.mkdir()
    for name, body in files.items():
        (folder / name).write_text(body)
    return tmp_path


def test_drop_folder_stores_files_with_published_time_from_sidecar(session, tmp_path):
    root = _drop(tmp_path, {"n50.csv": "a,b\n", "n50.csv.meta.json": META, ".DS_Store": ""})
    ctx = _ctx(session, drop_folder=root, source_switches={Drop.name: True})
    result = Drop().run(ctx)
    assert result.raw_files == 1
    [row] = _stored(session)
    assert row.as_of == PUBLISHED and row.fetched_at == FETCHED and row.source_url == URL


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"n50.csv": "a"}, DropFileError),
        ({"orphan.csv.meta.json": META}, DropFileError),
        ({"n50.csv": "a", "n50.csv.meta.json": META.replace("+05:30", "")}, ValidationError),
        ({"n50.csv": "a", "n50.csv.meta.json": META[:-1] + ', "note": "x"}'}, ValidationError),
    ],
    ids=["missing-sidecar", "orphan-sidecar", "naive-published-at", "unknown-field"],
)
def test_drop_folder_fails_rather_than_guessing_provenance(session, tmp_path, files, error):
    root = _drop(tmp_path, files)
    with pytest.raises(error):
        Drop().run(_ctx(session, drop_folder=root, source_switches={Drop.name: True}))
    assert _stored(session) == []


def test_drop_folder_unset_is_a_failure_not_a_skip(session):
    with pytest.raises(DropFileError, match="EQUITY_DROP_FOLDER"):
        Drop().run(_ctx(session, source_switches={Drop.name: True}))


def test_drop_adapter_is_manual_drop_class():
    assert Drop.source_class is SourceClass.MANUAL_DROP
