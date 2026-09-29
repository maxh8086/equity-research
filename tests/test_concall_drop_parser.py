"""Transcript file -> text, and the two facts its name carries. No database, no model."""

from datetime import date
from pathlib import Path

import pytest

from core.db.models import GuidanceIssueReason as Reason
from ingest.concall_drop.parser import (
    MIN_CHARS,
    RULE_VERSION,
    DocumentRejected,
    normalise,
    parse_filename,
    parse_transcript,
)
from tests.factories import pdf_bytes

FIXTURES = Path(__file__).parent / "fixtures" / "concall"
TRANSCRIPT = FIXTURES / "ACME_2026-07-24.txt"
PDF = "application/pdf"
TXT = "text/plain"


def parse(data: bytes, *, filename="ACME_2026-07-24.pdf", media_type=PDF):
    return parse_transcript(data, filename=filename, media_type=media_type)


def long_enough(marker: str = "") -> str:
    """Text over MIN_CHARS, so a test about something else does not trip the floor."""
    body = TRANSCRIPT.read_text(encoding="utf-8")
    return f"{marker}\n{body}" if marker else body


# --------------------------------------------------------------------------- #
# The file name is where the symbol and the call date come from
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "symbol", "call_date"),
    [
        ("ACME_2026-07-24.pdf", "ACME", date(2026, 7, 24)),
        ("ACME_2026-07-24.txt", "ACME", date(2026, 7, 24)),
        ("M&M_2025-07-29.pdf", "M&M", date(2025, 7, 29)),
        ("BAJAJ-AUTO_2025-07-24_q1.pdf", "BAJAJ-AUTO", date(2025, 7, 24)),
        ("NIFTY50_2025-01-02_call-2.pdf", "NIFTY50", date(2025, 1, 2)),
    ],
)
def test_the_convention_the_person_downloading_the_file_follows(name, symbol, call_date):
    assert parse_filename(name) == (symbol, call_date)


@pytest.mark.parametrize(
    "name",
    [
        "ACME.pdf",  # no date
        "acme_2026-07-24.pdf",  # symbols are upper case
        "ACME_24-07-2026.pdf",  # the other date order
        "ACME_2026-07-24.docx",  # not a transcript file
        "ACME_2026-13-01.pdf",  # not a date
        "ACME_2026-07-24.pdf.meta.json",
        " ACME_2026-07-24.pdf",
    ],
)
def test_a_name_off_the_convention_is_rejected_never_guessed_at(name):
    with pytest.raises(DocumentRejected) as exc:
        parse_filename(name)
    assert exc.value.reason is Reason.SHAPE_CHANGED


def test_nothing_inside_the_document_is_trusted_for_the_symbol():
    """The prose says Acme Industries Limited; the row says what the name says."""
    parsed = parse(long_enough().encode(), filename="XYZ_2026-07-24.txt", media_type=TXT)
    assert parsed.symbol == "XYZ"
    assert "ACME INDUSTRIES" in parsed.text


# --------------------------------------------------------------------------- #
# The text
# --------------------------------------------------------------------------- #


def test_page_breaks_become_blank_lines_and_trailing_space_goes():
    assert normalise("a  \nb\fc") == "a\nb\n\nc"
    assert normalise("a\r\nb\rc") == "a\nb\nc"
    assert normalise("a\n\n\n\n\n\nb") == "a\n\n\nb"
    assert normalise("\n  padded  \n") == "padded"


def test_a_line_break_inside_a_sentence_is_left_alone():
    """Reflowing would change the offsets every stored quote points into."""
    assert normalise("We expect EBITDA\nmargin of 15%.") == "We expect EBITDA\nmargin of 15%."


def test_a_text_transcript_reads_as_itself_with_no_pages():
    parsed = parse(TRANSCRIPT.read_bytes(), filename=TRANSCRIPT.name, media_type=TXT)
    assert parsed.page_count == 0
    assert "we will now begin the question-and-answer session" in parsed.text.lower()
    assert parsed.text == normalise(TRANSCRIPT.read_text(encoding="utf-8"))


def test_a_pdf_gives_its_text_layer_and_its_page_count():
    body = long_enough()
    halfway = body.index("\n", len(body) // 2)
    parsed = parse(pdf_bytes([body[:halfway], body[halfway:]]))
    assert parsed.page_count == 2
    assert "Ratnagiri" in parsed.text


def test_bytes_that_are_not_a_pdf_are_rejected_as_a_shape_change():
    with pytest.raises(DocumentRejected) as exc:
        parse(b"this is not a PDF at all")
    assert exc.value.reason is Reason.SHAPE_CHANGED


def test_text_that_is_not_utf8_is_rejected_rather_than_mangled():
    with pytest.raises(DocumentRejected) as exc:
        parse(b"\xff\xfe not utf-8", filename="ACME_2026-07-24.txt", media_type=TXT)
    assert exc.value.reason is Reason.SHAPE_CHANGED


@pytest.mark.parametrize("media_type", ["application/xml", "image/png", "application/octet-stream", ""])
def test_a_media_type_that_is_not_a_transcript_is_rejected(media_type):
    with pytest.raises(DocumentRejected) as exc:
        parse(b"whatever", media_type=media_type)
    assert exc.value.reason is Reason.SHAPE_CHANGED


# --------------------------------------------------------------------------- #
# A scan has nothing to extract from
# --------------------------------------------------------------------------- #


def test_a_scanned_transcript_is_quarantined_not_sent_to_a_model():
    """No OCR: a page of stray characters would be asked about and answered from nothing."""
    with pytest.raises(DocumentRejected) as exc:
        parse(pdf_bytes(["ACME INDUSTRIES LIMITED", ""]))
    assert exc.value.reason is Reason.NO_TEXT_LAYER
    assert str(MIN_CHARS) in exc.value.detail


def test_an_empty_text_file_is_quarantined_for_the_same_reason():
    with pytest.raises(DocumentRejected) as exc:
        parse(b"", filename="ACME_2026-07-24.txt", media_type=TXT)
    assert exc.value.reason is Reason.NO_TEXT_LAYER


def test_a_real_sized_transcript_clears_the_floor_by_a_wide_margin():
    """The floor catches scans, not short calls (tests/fixtures/concall/SOURCES.md)."""
    parsed = parse(TRANSCRIPT.read_bytes(), filename=TRANSCRIPT.name, media_type=TXT)
    assert len(parsed.text) > MIN_CHARS


def test_the_rule_version_is_stated_and_dated():
    assert RULE_VERSION.startswith("concall-text-")
