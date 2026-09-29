"""The ingest half of store 2: a transcript file becomes a company, a call date and text.

No model runs in any of these tests, and none is needed. Everything this
adapter decides -- which company, which call, whether there is anything to
read at all -- it decides from the file name, the sidecar and dated exchange
data (R1). The model half is tests/test_extract_concall.py.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from core.compute.hashing import content_hash
from core.config import Settings
from core.db.models import (
    ConcallDocument,
    GuidanceQuarantine,
    IndexCode,
    IndexSnapshot,
    IndexSnapshotConstituent,
    NseBhavcopyRow,
    XbrlIsinBasis,
)
from core.db.models import GuidanceIssueReason as Reason
from core.db.pit import concall_documents_as_of
from core.timezones import IST
from ingest.base import AdapterContext, DropFileError, RunStatus
from ingest.concall_drop.adapters import ConcallTranscriptDrop, drop_filename
from ingest.concall_drop.parser import RULE_VERSION
from tests.factories import pdf_bytes
from tests.fakes import MemoryBlobStore

pytestmark = pytest.mark.db

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "concall"
NAME = "concall_transcript_drop"
HOST = "https://www.acme.example/investors/"

ACME, OTHER = "INE002A01018", "INE009A01021"
CALL_DATE = date(2026, 7, 24)
FILE = "ACME_2026-07-24.txt"
PUBLISHED = datetime(2026, 7, 26, 18, 30, tzinfo=IST)
LOADED = PUBLISHED + timedelta(days=1)
BODY = (FIXTURES / FILE).read_bytes()


def _ctx(session, tmp_path, now=LOADED, blob=None, enabled=True) -> AdapterContext:
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: enabled})
    return AdapterContext(session, blob if blob is not None else MemoryBlobStore(), settings, now=lambda: now)


def _drop(
    tmp_path: Path,
    name: str = FILE,
    published_at: datetime = PUBLISHED,
    data: bytes | None = None,
    media_type: str = "text/plain",
    source_url: str | None = None,
) -> Path:
    folder = tmp_path / NAME
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(BODY if data is None else data)
    (folder / f"{name}.meta.json").write_text(
        json.dumps({"source_url": source_url or (HOST + name), "published_at": published_at.isoformat(),
                    "media_type": media_type})  # fmt: skip
    )
    return folder / name


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
        IndexSnapshotConstituent(snapshot_id=snapshot.id, isin=isin, symbol=symbol, series="EQ",
                                 company_name="Acme Industries Limited", industry="I", row_number=1, **prov)  # fmt: skip
    )
    session.flush()


def _quarantine(session) -> list[GuidanceQuarantine]:
    return list(session.scalars(select(GuidanceQuarantine).order_by(GuidanceQuarantine.id)))


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def test_a_dropped_transcript_becomes_one_document_row(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)

    result = ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert (result.raw_files, result.rows_written, result.quarantined) == (1, 1, 0)

    (document,) = concall_documents_as_of(session, as_of=LOADED)
    assert (document.isin, document.symbol, document.call_date) == (ACME, "ACME", CALL_DATE)
    assert document.isin_basis is XbrlIsinBasis.BHAVCOPY
    assert document.rule_version == RULE_VERSION
    assert document.page_count == 0 and document.char_count > 2000


def test_as_of_is_when_the_transcript_was_published_not_when_it_was_read(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)
    ConcallTranscriptDrop().run(_ctx(session, tmp_path, now=PUBLISHED + timedelta(days=40)))

    (document,) = concall_documents_as_of(session, as_of=PUBLISHED)
    assert document.as_of == PUBLISHED
    assert document.model_version is None  # nothing on this row came from a model


def test_a_document_is_invisible_to_a_read_before_it_was_published(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)
    ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert concall_documents_as_of(session, as_of=PUBLISHED - timedelta(seconds=1)) == []


def test_a_pdf_transcript_records_its_page_count(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    body = BODY.decode()
    halfway = body.index("\n", len(body) // 2)
    _drop(tmp_path, "ACME_2026-07-24.pdf", data=pdf_bytes([body[:halfway], body[halfway:]]),
          media_type="application/pdf")  # fmt: skip

    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path)).rows_written == 1
    (document,) = concall_documents_as_of(session, as_of=LOADED)
    assert document.page_count == 2


def test_the_same_file_is_not_loaded_twice(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)
    adapter = ConcallTranscriptDrop()
    assert adapter.run(_ctx(session, tmp_path)).rows_written == 1

    again = adapter.run(_ctx(session, tmp_path, now=LOADED + timedelta(days=1)))
    assert again.status is RunStatus.SUCCEEDED
    assert again.rows_written == 0
    assert "1 files already loaded" in again.detail
    assert len(concall_documents_as_of(session, as_of=LOADED + timedelta(days=1))) == 1


# --------------------------------------------------------------------------- #
# The ISIN is resolved, never guessed
# --------------------------------------------------------------------------- #


def test_an_index_list_alone_resolves_the_symbol(session, tmp_path):
    _index_list(session, "ACME", ACME, PUBLISHED - timedelta(days=30))
    _drop(tmp_path)
    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path)).rows_written == 1

    (document,) = concall_documents_as_of(session, as_of=LOADED)
    assert (document.isin, document.isin_basis) == (ACME, XbrlIsinBasis.INDEX_LIST)


def test_a_symbol_nothing_knows_is_quarantined_not_guessed(session, tmp_path):
    _drop(tmp_path)
    result = ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.FAILED
    assert concall_documents_as_of(session, as_of=LOADED) == []

    (held,) = _quarantine(session)
    assert held.reason is Reason.ISIN_UNRESOLVED
    assert held.isin is None and "ACME" in held.detail


def test_reads_that_disagree_are_a_conflict_not_a_casting_vote(session, tmp_path):
    """The same ticker has belonged to two companies; picking one would be a guess."""
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _index_list(session, "ACME", OTHER, PUBLISHED - timedelta(days=30))
    _drop(tmp_path)

    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path)).status is RunStatus.FAILED
    (held,) = _quarantine(session)
    assert held.reason is Reason.ISIN_CONFLICT
    assert ACME in held.detail and OTHER in held.detail


def test_a_stale_bhavcopy_row_outside_the_window_is_not_used(session, tmp_path):
    """Symbols are reused: a row from months ago may name another company."""
    _bhav(session, "ACME", ACME, date(2026, 1, 5))
    _drop(tmp_path)
    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path)).status is RunStatus.FAILED
    assert _quarantine(session)[0].reason is Reason.ISIN_UNRESOLVED


def test_a_bhavcopy_published_after_the_transcript_cannot_resolve_it(session, tmp_path):
    """R2: the resolution may only use what was knowable at the file's as_of."""
    _bhav(session, "ACME", ACME, date(2026, 8, 10))
    _drop(tmp_path)
    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path, now=datetime(2026, 8, 20, tzinfo=IST))).status is (
        RunStatus.FAILED
    )
    assert _quarantine(session)[0].reason is Reason.ISIN_UNRESOLVED


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #


def test_a_transcript_published_before_its_own_call_is_held_back(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 20))
    _drop(tmp_path, published_at=datetime(2026, 7, 20, 9, 0, tzinfo=IST))

    result = ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert result.status is RunStatus.FAILED
    (held,) = _quarantine(session)
    assert held.reason is Reason.IMPLAUSIBLE_AS_OF
    assert "before the call" in held.detail


def test_a_scan_is_held_back_with_the_bytes_still_stored(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path, "ACME_2026-07-24.pdf", data=pdf_bytes(["ACME INDUSTRIES LIMITED"]),
          media_type="application/pdf")  # fmt: skip

    result = ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert (result.raw_files, result.rows_written, result.quarantined) == (1, 0, 1)
    assert _quarantine(session)[0].reason is Reason.NO_TEXT_LAYER


def test_a_pre_model_rejection_names_no_model_at_all(session, tmp_path):
    """concall_document_loaded_as_of finds exactly these by extractor_version IS NULL."""
    _drop(tmp_path)
    ConcallTranscriptDrop().run(_ctx(session, tmp_path))

    (held,) = _quarantine(session)
    assert (held.extractor_version, held.prompt_hash, held.model_version, held.quote) == (None, None, None, None)
    assert held.rule_version == RULE_VERSION
    assert held.as_of == PUBLISHED


def test_the_same_rejection_is_recorded_once_but_retried_every_run(session, tmp_path):
    """A symbol becomes resolvable the day its bhavcopy lands; nobody touches the folder."""
    _drop(tmp_path)
    adapter = ConcallTranscriptDrop()
    assert adapter.run(_ctx(session, tmp_path)).quarantined == 1
    assert adapter.run(_ctx(session, tmp_path, now=LOADED + timedelta(days=1))).quarantined == 0
    assert len(_quarantine(session)) == 1

    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    later = adapter.run(_ctx(session, tmp_path, now=LOADED + timedelta(days=2)))
    assert later.status is RunStatus.SUCCEEDED
    assert later.rows_written == 1


# --------------------------------------------------------------------------- #
# Reparse: stored bytes, never a second download
# --------------------------------------------------------------------------- #


def test_reparse_reads_the_stored_bytes_with_the_drop_folder_gone(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    path = _drop(tmp_path)
    blob = MemoryBlobStore()
    adapter = ConcallTranscriptDrop()
    assert adapter.run(_ctx(session, tmp_path, blob=blob)).rows_written == 1

    path.unlink()
    path.with_name(path.name + ".meta.json").unlink()
    result = adapter.reparse(_ctx(session, tmp_path, now=LOADED + timedelta(days=1), blob=blob))
    assert result.status is RunStatus.SUCCEEDED, result.detail
    assert result.rows_written == 0  # already loaded under this RULE_VERSION
    assert "1 files already loaded" in result.detail


def test_a_reparse_under_a_new_rule_adds_a_row_at_the_original_as_of(session, tmp_path, monkeypatch):
    """R2: a parser fix never revises history; it adds a version of it."""
    from ingest.concall_drop import adapters as module

    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)
    blob = MemoryBlobStore()
    adapter = ConcallTranscriptDrop()
    adapter.run(_ctx(session, tmp_path, blob=blob))

    monkeypatch.setattr(module, "RULE_VERSION", "concall-text-9999-01-01")
    result = adapter.reparse(_ctx(session, tmp_path, now=LOADED + timedelta(days=1), blob=blob))
    assert result.rows_written == 1

    documents = concall_documents_as_of(session, as_of=LOADED + timedelta(days=1))
    assert [d.rule_version for d in documents] == [RULE_VERSION, "concall-text-9999-01-01"]
    assert {d.as_of for d in documents} == {PUBLISHED}


# --------------------------------------------------------------------------- #
# The file name has to survive into the store
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source_url", "expected"),
    [
        (HOST + FILE, FILE),
        (HOST + FILE + "?download=1", FILE),
        ("https://nsearchives.nseindia.com/corporate/xyz.zip#name=" + FILE, FILE),
    ],
)
def test_the_drop_name_is_recoverable_from_the_sidecar_url(source_url, expected):
    assert drop_filename(source_url) == expected


def test_a_url_that_does_not_give_the_name_back_fails_at_ingest_not_at_reparse(session, tmp_path):
    """Otherwise the file parses fine today and is rejected the day someone fixes the parser."""
    _drop(tmp_path, source_url="https://www.acme.example/investors/transcript-q1.txt")
    with pytest.raises(DropFileError) as exc:
        ConcallTranscriptDrop().run(_ctx(session, tmp_path))
    assert "#name=" in str(exc.value) and FILE in str(exc.value)


def test_a_fragment_naming_the_file_is_accepted(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path, source_url=f"https://www.acme.example/q1.zip#name={FILE}")
    assert ConcallTranscriptDrop().run(_ctx(session, tmp_path)).rows_written == 1


# --------------------------------------------------------------------------- #
# Switches and canary
# --------------------------------------------------------------------------- #


def test_a_switched_off_adapter_writes_nothing_and_is_not_a_failure(session, tmp_path):
    _drop(tmp_path)
    result = ConcallTranscriptDrop().run(_ctx(session, tmp_path, enabled=False))
    assert result.status is RunStatus.DISABLED
    assert session.scalar(select(ConcallDocument.id)) is None


def test_the_canary_reads_the_folder_without_writing_anything(session, tmp_path):
    _bhav(session, "ACME", ACME, date(2026, 7, 24))
    _drop(tmp_path)
    ConcallTranscriptDrop().canary(_ctx(session, tmp_path))
    assert session.scalar(select(ConcallDocument.id)) is None


def test_the_canary_fails_on_a_file_that_would_be_rejected(session, tmp_path):
    from ingest.concall_drop.parser import DocumentRejected

    _drop(tmp_path, "ACME_2026-07-24.pdf", data=pdf_bytes(["nothing here"]), media_type="application/pdf")
    with pytest.raises(DocumentRejected):
        ConcallTranscriptDrop().canary(_ctx(session, tmp_path))
