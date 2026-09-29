"""What the model says, checked against the document it read.

No model runs here: FakeProvider returns scripted JSON, so every test below
asks the same question -- given an answer of this shape, what does code do
with it? The answer that matters is that a claim which cannot be found in the
document, or whose number cannot be read, is held back with the words that
caused it rather than stored or repaired (R3).
"""

import json
from decimal import Decimal

import pytest

from core.compute.guidance import ClaimSection, HedgeStrength, Specificity, Unit
from extract.guidance import (
    EXTRACTOR_VERSION,
    INSTRUCTIONS,
    ClaimIssue,
    ClaimIssueReason,
    ExtractedClaim,
    ExtractionResult,
    Metric,
    ResolvedClaim,
    contains,
    extract_guidance,
    find_quote,
    locate_quote,
    qa_boundary,
    resolve,
)
from gateway import UNTRUSTED_DOCUMENT_GUARD, FakeProvider, ModelTier

TRANSCRIPT = """\
ACME INDUSTRIES LIMITED
Q2 FY26 Earnings Conference Call
October 24, 2025

Moderator: Ladies and gentlemen, good day and welcome to the Acme Industries
Limited Q2 FY26 earnings conference call.

Mr. R. Iyer -- Managing Director & CEO: Thank you. Revenue for the quarter was
Rs 1,240 crore, up 14% year on year. We expect EBITDA margin of 15-17% in FY27
as the new line ramps up. We are working towards a net debt of Rs 2,000 crore
by March 2027. We might conceivably reach 20% margin in FY29.
We remain focused on disciplined execution.

Moderator: We will now begin the question-and-answer session.

Analyst: Thank you for taking my question. What about capex?

Mr. R. Iyer -- Managing Director & CEO: We are targeting capex of Rs 900 crore
for FY27. We remain focused on disciplined execution.
"""

EXPECT_QUOTE = "We expect EBITDA margin of 15-17% in FY27 as the new line ramps up."
CAPEX_QUOTE = "We are targeting capex of Rs 900 crore for FY27."
REPEATED_QUOTE = "We remain focused on disciplined execution."
UNMAPPED_QUOTE = "We might conceivably reach 20% margin in FY29."

DEFAULTS = dict(
    quote=EXPECT_QUOTE,
    speaker_name="Mr. R. Iyer",
    speaker_role="Managing Director & CEO",
    section=ClaimSection.PREPARED_REMARKS,
    metric=Metric.EBITDA_MARGIN,
    metric_verbatim="EBITDA margin",
    period_label="FY27",
    value_text="15-17%",
    hedge_verbatim="We expect",
    is_forward_looking=True,
)


def claim(**overrides) -> ExtractedClaim:
    return ExtractedClaim(**{**DEFAULTS, **overrides})


def resolve_one(claim_: ExtractedClaim, transcript: str = TRANSCRIPT):
    claims, issues = resolve(ExtractionResult(claims=[claim_]), transcript=transcript)
    assert len(claims) + len(issues) == 1
    return claims[0] if claims else issues[0]


def reason_of(claim_: ExtractedClaim, transcript: str = TRANSCRIPT) -> ClaimIssueReason:
    outcome = resolve_one(claim_, transcript)
    assert isinstance(outcome, ClaimIssue), f"expected a held-back claim, got {outcome}"
    return outcome.reason


# --------------------------------------------------------------------------- #
# Finding the quote in the document
# --------------------------------------------------------------------------- #


def test_the_stored_location_points_at_the_quote_in_the_original_text():
    location = find_quote(TRANSCRIPT, EXPECT_QUOTE)
    assert TRANSCRIPT[location.start : location.end].split() == EXPECT_QUOTE.split()


def test_a_quote_broken_across_lines_by_the_pdf_still_matches():
    """A faithful copy out of a PDF rarely matches byte for byte."""
    wrapped = "We are targeting  capex of Rs 900\n   crore for FY27."
    assert find_quote(TRANSCRIPT, wrapped) is not None


def test_occurrences_are_counted_so_missing_and_ambiguous_stay_different_findings():
    assert locate_quote(TRANSCRIPT, REPEATED_QUOTE) == (None, 2)
    assert locate_quote(TRANSCRIPT, "We will double revenue next year.") == (None, 0)


def test_contains_ignores_whitespace_but_not_the_words():
    assert contains(EXPECT_QUOTE, "EBITDA\n  margin")
    assert not contains(EXPECT_QUOTE, "EBITDA margins")


def test_the_qa_boundary_is_read_from_the_transcript_not_from_the_model():
    boundary = qa_boundary(TRANSCRIPT)
    assert find_quote(TRANSCRIPT, CAPEX_QUOTE).start > boundary > find_quote(TRANSCRIPT, EXPECT_QUOTE).start


def test_a_transcript_that_marks_no_qa_session_has_no_boundary():
    assert qa_boundary("Mr. Iyer: We expect margins of 15% in FY27.") is None


# --------------------------------------------------------------------------- #
# A claim that survives every check
# --------------------------------------------------------------------------- #


def test_a_good_claim_is_graded_by_code_and_kept_verbatim():
    resolved = resolve_one(claim())
    assert isinstance(resolved, ResolvedClaim)
    assert resolved.hedge is HedgeStrength.EXPECT  # from the lexicon, not the model
    assert resolved.value.low == Decimal("15") and resolved.value.high == Decimal("17")
    assert resolved.value.unit is Unit.PERCENT
    assert resolved.value.specificity is Specificity.RANGE
    assert resolved.quote == EXPECT_QUOTE  # stored as the document prints it
    assert TRANSCRIPT[resolved.location.start : resolved.location.end].split() == EXPECT_QUOTE.split()


def test_a_qa_claim_is_kept_when_its_offset_agrees_with_the_transcript():
    resolved = resolve_one(
        claim(
            quote=CAPEX_QUOTE,
            section=ClaimSection.QA,
            metric=Metric.CAPEX,
            metric_verbatim="capex",
            value_text="Rs 900 crore",
            hedge_verbatim="We are targeting",
        )
    )
    assert isinstance(resolved, ResolvedClaim)
    assert resolved.hedge is HedgeStrength.AIM_TO
    assert resolved.value.unit is Unit.INR_CRORE
    assert resolved.section is ClaimSection.QA


def test_a_claim_with_no_number_is_kept_as_directional():
    resolved = resolve_one(
        claim(
            quote="We are working towards a net debt of Rs 2,000 crore by March 2027.",
            metric=Metric.NET_DEBT,
            metric_verbatim="net debt",
            period_label="March 2027",
            value_text=None,
            hedge_verbatim="We are working towards",
        )
    )
    assert isinstance(resolved, ResolvedClaim)
    assert resolved.value.specificity is Specificity.DIRECTIONAL
    assert resolved.hedge is HedgeStrength.WORKING_TOWARDS


# --------------------------------------------------------------------------- #
# Every reason a claim is held back
# --------------------------------------------------------------------------- #


def test_a_claim_the_model_marked_as_backward_looking_is_not_guidance():
    assert reason_of(claim(is_forward_looking=False)) is ClaimIssueReason.NOT_FORWARD_LOOKING


def test_other_is_a_question_for_a_person_not_a_bucket_to_store_in():
    assert reason_of(claim(metric=Metric.OTHER)) is ClaimIssueReason.UNMAPPED_METRIC


def test_an_invented_quote_is_held_back():
    invented = "We will double revenue in FY27, whatever the market does."
    assert reason_of(claim(quote=invented)) is ClaimIssueReason.QUOTE_NOT_IN_TRANSCRIPT


def test_a_quote_that_occurs_twice_cannot_be_stored_as_evidence():
    held = claim(
        quote=REPEATED_QUOTE,
        metric=Metric.CAPACITY,
        metric_verbatim="disciplined execution",
        value_text=None,
        hedge_verbatim="focused",
    )
    assert reason_of(held) is ClaimIssueReason.QUOTE_AMBIGUOUS


def test_a_hedge_the_speaker_did_not_say_is_held_back():
    assert reason_of(claim(hedge_verbatim="we will")) is ClaimIssueReason.HEDGE_NOT_IN_QUOTE


def test_a_number_that_is_not_in_the_quote_is_held_back():
    assert reason_of(claim(value_text="25%")) is ClaimIssueReason.VALUE_NOT_IN_QUOTE


def test_a_hedge_the_lexicon_does_not_know_is_held_back_for_review():
    held = claim(
        quote=UNMAPPED_QUOTE,
        metric_verbatim="margin",
        period_label="FY29",
        value_text="20%",
        hedge_verbatim="We might conceivably",
    )
    assert reason_of(held) is ClaimIssueReason.UNMAPPED_HEDGE


def test_a_number_the_parser_cannot_read_is_held_back_not_guessed():
    assert reason_of(claim(value_text="15-17% in FY27")) is ClaimIssueReason.VALUE_UNPARSED


def test_the_documents_own_qa_boundary_outranks_the_models_label():
    assert reason_of(claim(section=ClaimSection.QA)) is ClaimIssueReason.SECTION_MISMATCH


def test_where_the_transcript_marks_no_boundary_the_label_stands():
    short = "Mr. Iyer: We expect EBITDA margin of 15-17% in FY27 as the new line ramps up."
    assert isinstance(resolve_one(claim(section=ClaimSection.QA), short), ResolvedClaim)


def test_a_media_interview_is_not_checked_against_a_qa_boundary():
    resolved = resolve_one(claim(section=ClaimSection.MEDIA_INTERVIEW))
    assert isinstance(resolved, ResolvedClaim)


def test_every_reason_except_the_whole_document_one_is_reachable_from_a_claim():
    """SCHEMA_REJECTED is raised by the gateway, so no claim can carry it."""
    covered = {
        ClaimIssueReason.NOT_FORWARD_LOOKING,
        ClaimIssueReason.UNMAPPED_METRIC,
        ClaimIssueReason.QUOTE_NOT_IN_TRANSCRIPT,
        ClaimIssueReason.QUOTE_AMBIGUOUS,
        ClaimIssueReason.HEDGE_NOT_IN_QUOTE,
        ClaimIssueReason.VALUE_NOT_IN_QUOTE,
        ClaimIssueReason.UNMAPPED_HEDGE,
        ClaimIssueReason.VALUE_UNPARSED,
        ClaimIssueReason.SECTION_MISMATCH,
    }
    assert set(ClaimIssueReason) - covered == {ClaimIssueReason.SCHEMA_REJECTED}


def test_the_words_that_caused_the_issue_are_kept_with_it():
    outcome = resolve_one(claim(hedge_verbatim="we will"))
    assert outcome.quote == EXPECT_QUOTE
    assert "we will" in outcome.detail


# --------------------------------------------------------------------------- #
# The whole call
# --------------------------------------------------------------------------- #


def response(*claims: ExtractedClaim) -> str:
    return json.dumps({"claims": [json.loads(c.model_dump_json()) for c in claims]})


def test_the_transcript_reaches_the_model_only_as_a_fenced_document():
    provider = FakeProvider(responses=[response(claim())])
    extract_guidance(provider, TRANSCRIPT)
    prompt = provider.calls[0]["prompt"]
    assert UNTRUSTED_DOCUMENT_GUARD in prompt
    assert "ACME INDUSTRIES" not in INSTRUCTIONS
    assert prompt.index("ACME INDUSTRIES") > prompt.index("<<<BEGIN UNTRUSTED DOCUMENT>>>")


def test_the_outcome_carries_the_provenance_a_stored_row_needs():
    provider = FakeProvider(responses=[response(claim())], resolved_model="weights-2026-02-02")
    outcome = extract_guidance(provider, TRANSCRIPT)
    assert outcome.model_version == "weights-2026-02-02"
    assert outcome.extracted_by.startswith(f"{EXTRACTOR_VERSION}:")
    assert len(outcome.prompt_hash) == 64


def test_good_and_held_back_claims_come_back_from_one_call_together():
    provider = FakeProvider(responses=[response(claim(), claim(metric=Metric.OTHER))])
    outcome = extract_guidance(provider, TRANSCRIPT)
    assert len(outcome.claims) == 1
    assert [i.reason for i in outcome.issues] == [ClaimIssueReason.UNMAPPED_METRIC]


def test_the_tier_is_sonnet_unless_a_caller_says_otherwise():
    """CLAUDE.md Testing: a failing accuracy check reverts the tier, never lowers the bar."""
    provider = FakeProvider(responses=[response(), response()])
    extract_guidance(provider, TRANSCRIPT)
    extract_guidance(provider, TRANSCRIPT, model=ModelTier.OPUS)
    assert [c["model"] for c in provider.calls] == [ModelTier.SONNET.value, ModelTier.OPUS.value]


def test_a_document_with_no_guidance_is_an_empty_answer_not_an_error():
    outcome = extract_guidance(FakeProvider(responses=[response()]), TRANSCRIPT)
    assert outcome.claims == () and outcome.issues == ()


@pytest.mark.parametrize(
    "field",
    ["quote", "hedge_verbatim", "value_text", "metric_verbatim", "period_label", "speaker_name"],
)
def test_the_model_may_not_send_a_field_the_schema_does_not_declare(field):
    """extra="forbid": a smuggled field would never be checked against the document."""
    payload = json.loads(claim().model_dump_json())
    payload[f"{field}_note"] = "smuggled"
    with pytest.raises(Exception):
        ExtractedClaim(**payload)
