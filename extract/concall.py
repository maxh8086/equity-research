"""The model half of store 2: stored transcripts -> guidance_claim rows.

Runs as its own step, after `ingest/concall_drop` has stored the bytes and
decided the ISIN and call date by code. This step re-derives the text from
those same bytes -- it imports the ingest parser rather than keeping a second
copy of it, because the offsets stored on every claim are offsets into exactly
that string -- calls one model through the gateway, and writes only what code
could find in the document again.

Nothing here judges. The model points at sentences; `extract/guidance.py`
checks each one against the source and `core/compute/guidance.py` grades it
(R1). A claim that fails any check is quarantined with the words that caused
it, never repaired (R3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from core.blob import BlobStore
from core.db.models import ConcallDocument, ConcallTranscript, GuidanceClaim, GuidanceQuarantine
from core.db.models import GuidanceIssueReason as Reason
from core.db.pit import (
    concall_documents_as_of,
    concall_transcript_loaded_as_of,
    raw_source_file_as_of,
)
from extract.guidance import EXTRACTOR_VERSION, ClaimIssue, ExtractionOutcome, ResolvedClaim, extract_guidance
from gateway import ModelTier, Provider, SchemaRejected
from ingest.concall_drop.parser import RULE_VERSION as TEXT_RULE_VERSION
from ingest.concall_drop.parser import DocumentRejected, parse_transcript

# Claim-level reasons are the same words in both enums; the store's enum is the
# wider one, so the mapping is by value and a new reason in one and not the
# other fails here rather than silently landing as something else.
CLAIM_REASONS = {r.value for r in Reason}


@dataclass(frozen=True)
class ExtractionRun:
    documents: int = 0
    transcripts: int = 0
    claims: int = 0
    quarantined: int = 0
    already_extracted: int = 0

    def __add__(self, other: ExtractionRun) -> ExtractionRun:
        return ExtractionRun(
            self.documents + other.documents,
            self.transcripts + other.transcripts,
            self.claims + other.claims,
            self.quarantined + other.quarantined,
            self.already_extracted + other.already_extracted,
        )


def _reason(issue: ClaimIssue) -> Reason:
    if issue.reason.value not in CLAIM_REASONS:  # pragma: no cover - guarded by a test
        raise ValueError(f"{issue.reason} has no guidance_issue_reason; add it to the store's enum")
    return Reason(issue.reason.value)


def _claim_row(claim: ResolvedClaim, *, document: ConcallDocument, base: dict) -> GuidanceClaim:
    return GuidanceClaim(
        isin=document.isin,
        call_date=document.call_date,
        metric=claim.metric.value,
        metric_verbatim=claim.metric_verbatim,
        period_label=claim.period_label,
        quote=claim.quote,
        quote_start=claim.location.start,
        quote_end=claim.location.end,
        speaker_name=claim.speaker_name,
        speaker_role=claim.speaker_role,
        section=claim.section,
        hedge_verbatim=claim.hedge_verbatim,
        hedge_strength=claim.hedge,
        specificity=claim.value.specificity,
        value_text=claim.value_text,
        value_low=claim.value.low,
        value_high=claim.value.high,
        value_unit=claim.value.unit,
        **base,
    )


def store_outcome(
    session: Session,
    *,
    document: ConcallDocument,
    text: str,
    outcome: ExtractionOutcome,
    model_requested: str,
) -> ExtractionRun:
    """Write one extraction: a transcript row, its claims and its held-back claims.

    Pure of the model: everything it needs has already been decided. The
    transcript row carries the counts so a run can be audited without reading
    the claims back.
    """
    base = dict(
        as_of=document.as_of,
        content_hash=document.content_hash,
        source_url=document.source_url,
        extracted_by=outcome.extracted_by,
        model_version=outcome.model_version,
        prompt_hash=outcome.prompt_hash,
        extractor_version=EXTRACTOR_VERSION,
        rule_version=outcome.claims[0].rule_version if outcome.claims else TEXT_RULE_VERSION,
    )
    session.add(
        ConcallTranscript(
            isin=document.isin,
            symbol=document.symbol,
            call_date=document.call_date,
            fiscal_period=None,
            char_count=len(text),
            claims_written=len(outcome.claims),
            claims_quarantined=len(outcome.issues),
            model_requested=model_requested,
            **base,
        )
    )
    for claim in outcome.claims:
        session.add(_claim_row(claim, document=document, base=base))
    for issue in outcome.issues:
        session.add(
            GuidanceQuarantine(
                isin=document.isin, quote=issue.quote, reason=_reason(issue), detail=issue.detail, **base
            )
        )
    session.flush()
    return ExtractionRun(
        documents=1, transcripts=1, claims=len(outcome.claims), quarantined=len(outcome.issues)
    )


def transcript_text(session: Session, blob: BlobStore, *, document: ConcallDocument) -> str:
    """The exact string the ingest parser produced for this document, re-derived from the stored bytes.

    Not stored on the row: the bytes are the evidence, and a second copy of
    the text could drift from them. The file name is rebuilt from the document
    itself, so the text does not depend on what the drop folder still holds.
    """
    raw = raw_source_file_as_of(
        session, content_hash=document.content_hash, file_as_of=document.as_of, as_of=document.as_of
    )
    if raw is None:  # pragma: no cover - a document row without its file is a broken store
        raise DocumentRejected(Reason.SHAPE_CHANGED, f"no raw_source_file for {document.content_hash}")
    suffix = "txt" if raw.media_type == "text/plain" else "pdf"
    filename = f"{document.symbol}_{document.call_date.isoformat()}.{suffix}"
    return parse_transcript(blob.get(document.content_hash), filename=filename, media_type=raw.media_type).text


def extract_document(
    session: Session,
    blob: BlobStore,
    *,
    document: ConcallDocument,
    provider: Provider,
    model: ModelTier | str = ModelTier.SONNET,
) -> ExtractionRun:
    """One stored transcript through one model call, unless this setup already read it.

    The skip is checked before the call, not after: an unchanged document,
    extractor and tier has nothing new to say, and the cheapest model call is
    the one not made. The tier is compared as asked for -- `model_requested`,
    not the snapshot that answered, which is not knowable until the call has
    been paid for. A prompt edit therefore has to bump EXTRACTOR_VERSION,
    which sits in the same module as the prompt; tests/test_extract_concall.py
    holds that pair together.
    """
    extracted, _ = concall_transcript_loaded_as_of(
        session,
        content_hash=document.content_hash,
        file_as_of=document.as_of,
        extractor_version=EXTRACTOR_VERSION,
        model_requested=str(model),
        as_of=document.as_of,
    )
    if extracted:
        return ExtractionRun(documents=1, already_extracted=1)
    text = transcript_text(session, blob, document=document)
    outcome = extract_guidance(provider, text, model=model)
    return store_outcome(
        session, document=document, text=text, outcome=outcome, model_requested=str(model)
    )


def extract_pending(
    session: Session,
    blob: BlobStore,
    *,
    provider: Provider,
    as_of: datetime,
    isin: str | None = None,
    model: ModelTier | str = ModelTier.SONNET,
) -> ExtractionRun:
    """Every transcript known at `as_of` that this extractor and tier have not read yet.

    A document whose text the model could not be asked about at all -- an
    unreadable file, a rejected schema -- is quarantined whole and the run
    continues: one bad transcript does not stop the rest.
    """
    run = ExtractionRun()
    for document in concall_documents_as_of(session, isin=isin, as_of=as_of):
        try:
            run += extract_document(session, blob, document=document, provider=provider, model=model)
        except (DocumentRejected, SchemaRejected) as exc:
            reason = exc.reason if isinstance(exc, DocumentRejected) else Reason.SCHEMA_REJECTED
            session.add(
                GuidanceQuarantine(
                    isin=document.isin, quote=None, reason=reason, detail=str(exc), prompt_hash=None,
                    extractor_version=EXTRACTOR_VERSION, as_of=document.as_of,
                    content_hash=document.content_hash, source_url=document.source_url,
                    extracted_by=EXTRACTOR_VERSION, model_version=None, rule_version=TEXT_RULE_VERSION,
                )  # fmt: skip
            )
            session.flush()
            run += ExtractionRun(documents=1, quarantined=1)
    return run
