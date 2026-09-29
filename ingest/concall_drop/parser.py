"""Transcript file -> text, plus the two facts the file name carries.

A concall transcript PDF has no structured header: nothing inside it states an
ISIN, and the company name printed on page one is prose. So the symbol and the
call date come from the drop file's name, under a convention the person
downloading it follows, and the symbol is resolved to an ISIN by code against
dated exchange data (R1) -- never by matching the prose.

The text layer is kept as the PDF gives it, with only page breaks normalised.
Quotes are located in this text by `extract/guidance.py`, and the offsets it
stores point into exactly the string this module returns, so a change in here
is a change of `RULE_VERSION`.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date

from core.db.models import GuidanceIssueReason as Reason

# Bump when the text this module produces changes: stored quote offsets are
# offsets into that text.
RULE_VERSION = "concall-text-2026-09-29"

# <SYMBOL>_<YYYY-MM-DD>[_<note>].<ext> -- NSE symbols are upper case and may
# carry & or -, e.g. M&M_2025-07-29.pdf, BAJAJ-AUTO_2025-07-24_q1.pdf.
FILENAME = re.compile(r"^(?P<symbol>[A-Z0-9&-]{1,20})_(?P<date>\d{4}-\d{2}-\d{2})(?:_[\w-]+)?\.(?P<ext>pdf|txt)$")

TEXT_TYPES = frozenset({"text/plain"})
PDF_TYPES = frozenset({"application/pdf"})

# A scanned transcript yields a handful of stray characters at most. Below this
# there is nothing to extract from, and a model would be asked to read an empty
# page -- quarantine it for a person instead.
MIN_CHARS = 2000

_FORM_FEED = re.compile(r"\f+")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_MANY_BLANK_LINES = re.compile(r"\n{4,}")


class DocumentRejected(Exception):
    """Nothing from this file enters a store until a person looks at it."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TranscriptDocument:
    symbol: str
    call_date: date
    text: str
    page_count: int


def parse_filename(name: str) -> tuple[str, date]:
    """The symbol and call date the drop file's name declares. Raises if it does not follow the convention."""
    match = FILENAME.match(name)
    if match is None:
        raise DocumentRejected(
            Reason.SHAPE_CHANGED,
            f"{name!r} is not <SYMBOL>_<YYYY-MM-DD>[_<note>].pdf|.txt",
        )
    try:
        call_date = date.fromisoformat(match["date"])
    except ValueError as exc:
        raise DocumentRejected(Reason.SHAPE_CHANGED, f"{name!r}: {exc}") from exc
    return match["symbol"], call_date


def normalise(text: str) -> str:
    """Page breaks to blank lines, trailing spaces gone, runs of blank lines capped.

    Deliberately conservative: line breaks inside a sentence are left alone, so
    a quote is matched whitespace-insensitively rather than by reflowing the
    document and hoping the reflow matches what the model was shown.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _FORM_FEED.sub("\n\n", text)
    text = _TRAILING_SPACE.sub("\n", text)
    return _MANY_BLANK_LINES.sub("\n\n\n", text).strip()


def _pdf_text(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:  # pragma: no cover - a broken install, not data
        raise RuntimeError("pypdf is required to read transcript PDFs") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise DocumentRejected(Reason.SHAPE_CHANGED, f"cannot read the PDF: {exc}") from exc
    return "\n\n".join(pages), len(pages)


def parse_transcript(data: bytes, *, filename: str, media_type: str) -> TranscriptDocument:
    """One transcript file as text, or a rejection naming what a person has to fix."""
    symbol, call_date = parse_filename(filename)
    if media_type in PDF_TYPES:
        raw, page_count = _pdf_text(data)
    elif media_type in TEXT_TYPES:
        try:
            raw = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentRejected(Reason.SHAPE_CHANGED, f"not UTF-8 text: {exc}") from exc
        page_count = 0
    else:
        raise DocumentRejected(Reason.SHAPE_CHANGED, f"media type {media_type!r} is not a transcript")

    text = normalise(raw)
    if len(text) < MIN_CHARS:
        raise DocumentRejected(
            Reason.NO_TEXT_LAYER,
            f"{len(text)} characters of text in {page_count} pages, below the {MIN_CHARS} needed to extract from",
        )
    return TranscriptDocument(symbol=symbol, call_date=call_date, text=text, page_count=page_count)
