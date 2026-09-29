"""Stored transcripts through a scripted model, into the store.

FakeProvider answers every call here, so nothing in this file costs anything
or depends on a model behaving. What is under test is the part around the
call: that a document is not sent twice, that a document the model could not
be asked about at all is quarantined whole and the run carries on, and that
what lands in the store can be traced back to the bytes it came from.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from core.config import Settings
from core.db.models import ConcallDocument, GuidanceClaim, GuidanceQuarantine
from core.db.models import GuidanceIssueReason as Reason
from core.db.pit import concall_transcripts_as_of, guidance_claims_as_of
from extract.concall import extract_document, extract_pending, transcript_text
from extract.guidance import EXTRACTOR_VERSION, INSTRUCTIONS, ExtractedClaim, Metric
from gateway import UNTRUSTED_DOCUMENT_GUARD, FakeProvider, ModelTier
from ingest.base import AdapterContext
from ingest.concall_drop.adapters import ConcallTranscriptDrop
from ingest.concall_drop.parser import RULE_VERSION as TEXT_RULE_VERSION
from tests.factories import pdf_bytes
from tests.fakes import MemoryBlobStore
from tests.test_concall_drop_adapters import ACME, FILE, PUBLISHED, _bhav, _drop

pytestmark = pytest.mark.db

NAME = "concall_transcript_drop"
CALL_DATE = date(2026, 7, 24)
LATER = PUBLISHED + timedelta(days=1)
SOURCE = (Path(__file__).parent / "fixtures" / "concall" / FILE).read_text(encoding="utf-8")
QUOTE = "We expect EBITDA margin of 15-17% in FY28 as the"
UNSUPPORTED = "We will double revenue next year."


def claim(**overrides) -> ExtractedClaim:
    return ExtractedClaim(
        **{
            "quote": QUOTE,
            "speaker_name": "Mr. R. Iyer",
            "speaker_role": "Managing Director & Chief Executive Officer",
            "section": "prepared_remarks",
            "metric": Metric.EBITDA_MARGIN,
            "metric_verbatim": "EBITDA margin",
            "period_label": "FY28",
            "value_text": "15-17%",
            "hedge_verbatim": "We expect",
            "is_forward_looking": True,
            **overrides,
        }
    )


def answer(*claims: ExtractedClaim) -> str:
    return json.dumps({"claims": [json.loads(c.model_dump_json()) for c in claims]})


def _ingest(session, tmp_path, blob, **drop):
    _drop(tmp_path, **drop)
    settings = Settings(drop_folder=tmp_path, source_switches={NAME: True})
    return ConcallTranscriptDrop().run(AdapterContext(session, blob, settings, now=lambda: LATER))


def loaded(session, tmp_path, **drop) -> tuple[ConcallDocument, MemoryBlobStore]:
    """One transcript through the ingest adapter: a document row, a raw file and its bytes."""
    _bhav(session, "ACME", ACME, CALL_DATE)
    blob = MemoryBlobStore()
    assert _ingest(session, tmp_path, blob, **drop).rows_written == 1
    return session.scalars(select(ConcallDocument)).one(), blob


def _quarantine(session) -> list[GuidanceQuarantine]:
    return list(session.scalars(select(GuidanceQuarantine).order_by(GuidanceQuarantine.id)))


# --------------------------------------------------------------------------- #
# The text the model is asked about is the text the offsets point into
# --------------------------------------------------------------------------- #


def test_the_text_is_re_derived_from_the_stored_bytes_not_from_the_drop_folder(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    for path in (tmp_path / NAME).iterdir():
        path.unlink()

    text = transcript_text(session, blob, document=document)
    assert QUOTE in text
    assert len(text) == document.char_count


def test_a_pdf_document_is_re_read_through_the_pdf_path(session, tmp_path):
    document, blob = loaded(
        session, tmp_path, name="ACME_2026-07-24.pdf", data=pdf_bytes([SOURCE]), media_type="application/pdf"
    )
    assert document.page_count == 1
    assert "Ratnagiri" in transcript_text(session, blob, document=document)


# --------------------------------------------------------------------------- #
# One document, one call
# --------------------------------------------------------------------------- #


def test_a_claim_the_document_supports_is_stored_with_its_location(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim())])

    run = extract_document(session, blob, document=document, provider=provider, model=ModelTier.SONNET)
    assert (run.documents, run.transcripts, run.claims, run.quarantined) == (1, 1, 1, 0)

    (stored,) = guidance_claims_as_of(session, isin=ACME, as_of=LATER)
    text = transcript_text(session, blob, document=document)
    assert text[stored.quote_start : stored.quote_end].split() == QUOTE.split()
    assert stored.call_date == CALL_DATE
    assert stored.as_of == PUBLISHED  # the document's, not the extraction's


def test_the_transcript_row_carries_the_counts_and_both_model_strings(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim(), claim(quote=UNSUPPORTED))])
    extract_document(session, blob, document=document, provider=provider)

    (transcript,) = concall_transcripts_as_of(session, isin=ACME, as_of=LATER)
    assert (transcript.claims_written, transcript.claims_quarantined) == (1, 1)
    assert transcript.model_requested == str(ModelTier.SONNET)
    assert transcript.model_version == provider.resolved_model
    assert transcript.model_requested != transcript.model_version
    assert transcript.extractor_version == EXTRACTOR_VERSION
    assert transcript.char_count == document.char_count


def test_the_same_document_is_not_paid_for_twice(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim())])
    extract_document(session, blob, document=document, provider=provider)

    again = extract_document(session, blob, document=document, provider=provider)
    assert (again.documents, again.already_extracted, again.transcripts) == (1, 1, 0)
    assert len(provider.calls) == 1, "the skip is checked before the call, not after"
    assert len(concall_transcripts_as_of(session, isin=ACME, as_of=LATER)) == 1


def test_asking_a_different_tier_is_a_new_extraction_not_a_skip(session, tmp_path):
    """The stored model_version is the snapshot that answered, which no caller knows beforehand."""
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim()), answer(claim())])
    extract_document(session, blob, document=document, provider=provider, model=ModelTier.SONNET)
    extract_document(session, blob, document=document, provider=provider, model=ModelTier.OPUS)

    rows = concall_transcripts_as_of(session, isin=ACME, as_of=LATER)
    assert [r.model_requested for r in rows] == [str(ModelTier.SONNET), str(ModelTier.OPUS)]
    assert {r.as_of for r in rows} == {PUBLISHED}  # R2: a re-read adds a row, never revises one


def test_a_held_back_claim_keeps_the_words_that_caused_it(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim(quote=UNSUPPORTED))])
    extract_document(session, blob, document=document, provider=provider)

    (held,) = _quarantine(session)
    assert held.reason is Reason.QUOTE_NOT_IN_TRANSCRIPT
    assert held.quote == UNSUPPORTED
    assert held.isin == ACME and held.extractor_version == EXTRACTOR_VERSION
    assert session.scalar(select(GuidanceClaim.id)) is None


def test_a_call_that_promised_nothing_is_an_extraction_not_a_failure(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    run = extract_document(session, blob, document=document, provider=FakeProvider(responses=[answer()]))

    assert (run.transcripts, run.claims, run.quarantined) == (1, 0, 0)
    (transcript,) = concall_transcripts_as_of(session, isin=ACME, as_of=LATER)
    assert transcript.claims_written == 0
    assert transcript.rule_version == TEXT_RULE_VERSION


# --------------------------------------------------------------------------- #
# A run over everything pending
# --------------------------------------------------------------------------- #


def test_extract_pending_reads_only_what_was_published_by_as_of(session, tmp_path):
    _, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim())])

    early = extract_pending(session, blob, provider=provider, as_of=PUBLISHED - timedelta(days=1))
    assert early.documents == 0
    assert provider.calls == []


def test_a_document_the_model_cannot_be_asked_about_is_quarantined_whole(session, tmp_path):
    """A refused answer is not repaired and does not stop the run (R3)."""
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=["{not json at all"])

    run = extract_pending(session, blob, provider=provider, as_of=LATER)
    assert (run.documents, run.quarantined, run.transcripts) == (1, 1, 0)

    (held,) = _quarantine(session)
    assert held.reason is Reason.SCHEMA_REJECTED
    assert held.quote is None and held.isin == document.isin
    assert held.extractor_version == EXTRACTOR_VERSION
    assert held.model_version is None  # no answer was accepted, so there are no weights to name
    assert held.as_of == PUBLISHED


def test_one_bad_transcript_does_not_stop_the_others(session, tmp_path):
    _bhav(session, "ACME", ACME, CALL_DATE)
    _bhav(session, "ZED", "INE009A01021", CALL_DATE)
    blob = MemoryBlobStore()
    _drop(tmp_path, name="ZED_2026-07-24.txt", data=(SOURCE + "\nZed Limited.\n").encode())
    assert _ingest(session, tmp_path, blob).rows_written == 2

    provider = FakeProvider(responses=["{not json at all", answer(claim())])
    run = extract_pending(session, blob, provider=provider, as_of=LATER)
    assert (run.documents, run.quarantined, run.transcripts, run.claims) == (2, 1, 1, 1)


def test_a_second_pass_over_the_pending_set_sends_nothing_again(session, tmp_path):
    _, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim())])
    extract_pending(session, blob, provider=provider, as_of=LATER)

    again = extract_pending(session, blob, provider=provider, as_of=LATER)
    assert (again.documents, again.already_extracted) == (1, 1)
    assert len(provider.calls) == 1


# --------------------------------------------------------------------------- #
# The prompt and the version that names it
# --------------------------------------------------------------------------- #


def test_the_extractor_version_is_dated_and_sits_beside_the_prompt_it_describes():
    """A prompt edit that leaves this alone would silently reuse the old extraction."""
    assert EXTRACTOR_VERSION.startswith("guidance-extract-")
    assert "record" in INSTRUCTIONS  # same module, so the pair moves together


def test_the_document_reaches_the_model_only_inside_the_fence(session, tmp_path):
    document, blob = loaded(session, tmp_path)
    provider = FakeProvider(responses=[answer(claim())])
    extract_document(session, blob, document=document, provider=provider)

    (call,) = provider.calls
    prompt = call["prompt"]
    assert UNTRUSTED_DOCUMENT_GUARD in prompt
    assert prompt.index(UNTRUSTED_DOCUMENT_GUARD) < prompt.index("ACME INDUSTRIES")
