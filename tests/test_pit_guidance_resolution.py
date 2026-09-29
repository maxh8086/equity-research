"""Guidance graded, and judged silent, at t from what was public at t.

Nothing here is stored: each read recomputes from rows with as_of <= t, so the
same claim is OPEN before the results, MET after them, and MISSED once a
restatement lands, and a read made earlier never sees the later rows.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from core.compute.guidance import ClaimSection, HedgeStrength, Specificity, Unit
from core.compute.guidance_resolution import RULE_VERSION, Basis, Status, UnresolvableReason
from core.compute.hashing import content_hash
from core.db.models import (
    ConcallTranscript,
    Consolidation,
    FinancialFiling,
    GuidanceClaim,
    XbrlIsinBasis,
    XbrlTaxonomy,
)
from core.db.pit import guidance_delivery_as_of, guidance_resolutions_as_of, guidance_silence_as_of
from core.timezones import IST
from tests.factories import RELIANCE, make_fact

pytestmark = pytest.mark.db

D = Decimal
CR = D("10000000")  # one crore, in rupees
CONS, STAND = Consolidation.CONSOLIDATED, Consolidation.STANDALONE
FY25 = (date(2024, 4, 1), date(2025, 3, 31))
FY26 = (date(2025, 4, 1), date(2026, 3, 31))


def ist(year: int, month: int, day: int, hour: int = 10) -> datetime:
    return datetime(year, month, day, hour, tzinfo=IST)


CLAIM_CALL, CLAIM_PUBLISHED = date(2025, 5, 10), ist(2025, 5, 12)
RESULTS = ist(2026, 4, 25, 16)  # FY26 results filed
RESTATED = ist(2026, 8, 1)
BEFORE_RESULTS = ist(2026, 3, 1)
AFTER_RESULTS = ist(2026, 5, 1)
AFTER_RESTATEMENT = ist(2026, 9, 1)

_counter = iter(range(1, 10**6))


def _filing(
    session, name: str, as_of: datetime, period, *, taxonomy=XbrlTaxonomy.IND_AS, consolidation=CONS
) -> str:
    """A financial_filing row (it names the taxonomy family); returns the hash its facts carry."""
    digest = content_hash(f"{name}{as_of}{consolidation}".encode())
    session.add(
        FinancialFiling(
            isin=RELIANCE, isin_basis=XbrlIsinBasis.BHAVCOPY, symbol="RELIANCE", scrip_code=None,
            consolidation=consolidation, taxonomy=taxonomy, reporting_quarter="Q4", period_start=period[0],
            period_end=period[1], board_meeting_date=as_of.date(), facts_written=1, facts_quarantined=0,
            dimensional_facts_deferred=0, rule_version="tests-1", as_of=as_of, content_hash=digest,
            source_url=f"https://example.test/{name}.xml", extracted_by="tests", model_version=None,
        )  # fmt: skip
    )
    return digest


def _report(
    session, name: str, as_of: datetime, period, *, taxonomy=XbrlTaxonomy.IND_AS, consolidation=CONS, **crore
) -> None:
    """A filing and its duration facts; values are given in crore."""
    digest = _filing(session, name, as_of, period, taxonomy=taxonomy, consolidation=consolidation)
    session.add_all(
        make_fact(
            line_item=item, xbrl_element=f"t:{item}", period_start=period[0], period_end=period[1],
            value=D(value) * CR, as_of=as_of, content_hash=digest, consolidation=consolidation,
        )  # fmt: skip
        for item, value in crore.items()
    )
    session.flush()


def spec(metric, period, low, high=None, *, unit=Unit.INR_CRORE, hedge=HedgeStrength.EXPECT):
    """The claim columns the grader reads. One number is a point, two a range."""
    high = low if high is None else high
    return dict(
        metric=metric, period_label=period, value_low=D(low), value_high=D(high), value_unit=unit,
        specificity=Specificity.POINT if low == high else Specificity.RANGE, hedge_strength=hedge,
    )  # fmt: skip


def _call(
    session, name: str, call_date: date, published: datetime, *claims: dict,
    quarantined: int = 0, model: str = "model-1", prompt: str = "a" * 64,
) -> str:  # fmt: skip
    """One extracted transcript: its row and its claims, all at the document own as_of."""
    digest = content_hash(name.encode())
    provenance = dict(
        as_of=published, content_hash=digest, source_url=f"https://example.test/{name}.pdf",
        extracted_by="tests", model_version=model, prompt_hash=prompt, extractor_version="tests-1",
        rule_version="tests-1",
    )  # fmt: skip
    session.add(
        ConcallTranscript(
            isin=RELIANCE, symbol="RELIANCE", call_date=call_date, fiscal_period=None, char_count=1000,
            claims_written=len(claims), claims_quarantined=quarantined, model_requested="tier-1", **provenance,
        )
    )
    for claim in claims:
        start = next(_counter) * 100
        session.add(
            GuidanceClaim(
                isin=RELIANCE, call_date=call_date, metric_verbatim=claim["metric"], quote=f"quote {start}",
                quote_start=start, quote_end=start + 50, speaker_name="A. Speaker", speaker_role="CFO",
                section=ClaimSection.PREPARED_REMARKS, hedge_verbatim="we expect",
                value_text=f"{claim['value_low']}", **claim, **provenance,
            )  # fmt: skip
        )
    session.flush()
    return digest


def _read(session, as_of, **kw):
    return guidance_resolutions_as_of(session, isin=RELIANCE, as_of=as_of, **kw)


def _one(session, as_of, **kw):
    (row,) = _read(session, as_of, **kw)
    return row.resolution


@pytest.fixture
def fy26_revenue_guidance(session):
    """Revenue of 4,000-5,000 crore for FY26, guided in May 2025; nothing reported yet."""
    _call(session, "q4-fy25-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 4000, 5000))


# --------------------------------------------------------------------------- #
# Point in time
# --------------------------------------------------------------------------- #


def test_before_the_transcript_is_public_nothing_is_seen(session, fy26_revenue_guidance):
    just_before = ist(2025, 5, 12, 9)
    assert _read(session, just_before) == []
    assert guidance_silence_as_of(session, isin=RELIANCE, as_of=just_before) == []
    delivery = guidance_delivery_as_of(session, isin=RELIANCE, as_of=just_before)
    assert delivery.rate is None
    assert (delivery.met, delivery.missed, delivery.open, delivery.unresolvable, delivery.silent) == (0,) * 5


def test_the_claim_is_open_until_the_results_are_public_then_met(session, fy26_revenue_guidance):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)

    open_ = _one(session, BEFORE_RESULTS)
    assert (open_.status, open_.reason, open_.actual) == (Status.OPEN, None, None)

    met = _one(session, AFTER_RESULTS)
    assert (met.status, met.reason, met.actual) == (Status.MET, None, D("4500"))
    assert met.inputs == (("revenue_from_operations", FY26[1], D("4500") * CR),)
    assert met.rule_version == RULE_VERSION


def test_the_result_can_be_missed(session, fy26_revenue_guidance):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=3900)
    assert _one(session, AFTER_RESULTS).status is Status.MISSED


def test_a_restatement_after_t_does_not_change_the_answer_at_t(session, fy26_revenue_guidance):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)
    _report(session, "fy26-restated", RESTATED, FY26, revenue_from_operations=3900)

    at_t1 = _one(session, AFTER_RESULTS)
    at_t2 = _one(session, AFTER_RESTATEMENT)

    assert (at_t1.status, at_t1.actual) == (Status.MET, D("4500"))
    assert (at_t2.status, at_t2.actual) == (Status.MISSED, D("3900"))
    # Reading t1 again after the restatement is stored still gives the t1 answer.
    assert _one(session, AFTER_RESULTS) == at_t1


def test_a_claim_made_after_the_results_was_not_a_forecast(session):
    _report(session, "fy25", ist(2025, 4, 25, 16), FY25, revenue_from_operations=4200)
    _call(session, "q4-fy25-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY25", 4000, 5000))

    resolution = _one(session, AFTER_RESULTS)
    assert resolution.status is Status.UNRESOLVABLE
    assert resolution.reason is UnresolvableReason.ALREADY_KNOWN_WHEN_MADE
    assert resolution.actual == D("4200")


def test_a_fact_public_after_the_claim_does_not_make_it_already_known(session, fy26_revenue_guidance):
    # The FY26 results (April 2026) are newer than the claim (May 2025).
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)
    assert _one(session, AFTER_RESULTS).reason is None


# --------------------------------------------------------------------------- #
# Which facts
# --------------------------------------------------------------------------- #


def test_consolidated_is_preferred_over_standalone_and_recorded(session, fy26_revenue_guidance):
    _report(session, "fy26-standalone", RESULTS, FY26, consolidation=STAND, revenue_from_operations=3900)
    _report(session, "fy26-consolidated", RESULTS, FY26, consolidation=CONS, revenue_from_operations=4500)

    resolution = _one(session, AFTER_RESULTS)
    assert (resolution.status, resolution.basis, resolution.actual) == (Status.MET, Basis.CONSOLIDATED, D("4500"))


def test_standalone_is_used_when_there_is_no_consolidated_and_says_so(session, fy26_revenue_guidance):
    _report(session, "fy26-standalone", RESULTS, FY26, consolidation=STAND, revenue_from_operations=3900)

    resolution = _one(session, AFTER_RESULTS)
    assert (resolution.status, resolution.basis) == (Status.MISSED, Basis.STANDALONE)


def test_a_balance_sheet_claim_stays_open_when_only_the_profit_and_loss_is_public(session):
    _call(session, "call", CLAIM_CALL, CLAIM_PUBLISHED, spec("net_debt", "FY26", 1000))
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)

    # No balance sheet is stored for the year, so nothing can be graded; it is not repaired.
    assert _one(session, AFTER_RESULTS).status is Status.OPEN


def test_the_metric_filter_narrows_the_claims(session):
    _call(
        session, "call", CLAIM_CALL, CLAIM_PUBLISHED,
        spec("revenue", "FY26", 4000, 5000), spec("pat", "FY26", 500, 600),
    )  # fmt: skip
    assert {r.claim.metric for r in _read(session, AFTER_RESULTS)} == {"revenue", "pat"}
    assert {r.claim.metric for r in _read(session, AFTER_RESULTS, metric="pat")} == {"pat"}


# --------------------------------------------------------------------------- #
# A filing family the resolvers do not read
# --------------------------------------------------------------------------- #


def test_bank_facts_are_never_read_as_ind_as_and_the_claim_says_why(session, fy26_revenue_guidance):
    # A bank files in a different taxonomy: its line items are not the Ind AS ones the
    # resolvers are defined on, so even a same-named fact must not be graded against.
    _report(session, "bank-fy26", RESULTS, FY26, taxonomy=XbrlTaxonomy.BANK, revenue_from_operations=4500)

    resolution = _one(session, AFTER_RESULTS)
    assert resolution.status is Status.UNRESOLVABLE
    assert resolution.reason is UnresolvableReason.METRIC_NOT_IN_XBRL
    assert resolution.actual is None and resolution.inputs == ()


def test_before_a_bank_reports_anything_the_claim_is_just_open(session, fy26_revenue_guidance):
    assert _one(session, BEFORE_RESULTS).status is Status.OPEN


def test_ind_as_facts_win_when_the_company_has_both_families(session, fy26_revenue_guidance):
    _report(session, "old-bank", ist(2025, 4, 25, 16), FY25, taxonomy=XbrlTaxonomy.BANK, revenue_from_operations=1)
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)

    assert _one(session, AFTER_RESULTS).status is Status.MET


# --------------------------------------------------------------------------- #
# Re-extraction
# --------------------------------------------------------------------------- #


def test_a_re_extraction_of_the_same_transcript_is_not_counted_twice(session):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)
    # The same document, read again under a newer model: the earlier run claims stay in the
    # store, but only the extraction that stands for the document is read.
    _call(session, "call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 4000, 5000), model="model-1")
    _call(session, "call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 4600, 5000), model="model-2")

    (row,) = _read(session, AFTER_RESULTS)
    assert row.claim.model_version == "model-2"
    assert row.resolution.status is Status.MISSED  # the newer reading (4,600 or more) is what stands

    delivery = guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESULTS)
    assert (delivery.met, delivery.missed) == (0, 1)


# --------------------------------------------------------------------------- #
# SILENT
# --------------------------------------------------------------------------- #

T1_CALL, T1_PUBLISHED = date(2025, 8, 10), ist(2025, 8, 12)
T2_CALL, T2_PUBLISHED = date(2025, 11, 10), ist(2025, 11, 12)
JUST_BEFORE_T2 = ist(2025, 11, 12, 9)


def _silent(session, as_of, *, metric="revenue"):
    rows = [
        r for r in guidance_silence_as_of(session, isin=RELIANCE, as_of=as_of)
        if r.claim.metric == metric and r.claim.call_date == CLAIM_CALL
    ]  # fmt: skip
    (row,) = rows
    return row.silent


@pytest.fixture
def fy27_guidance(session):
    """A claim about a year that is still running, and a first later call that says nothing of it."""
    _call(session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY27", 5000, 6000))
    _call(session, "q1-call", T1_CALL, T1_PUBLISHED, spec("pat", "FY27", 500, 600))


def test_silence_appears_only_after_the_second_clean_later_call_is_public(session, fy27_guidance):
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("pat", "FY27", 500, 600))

    assert _silent(session, ist(2025, 5, 13)) is False  # no later call at all
    assert _silent(session, ist(2025, 8, 13)) is False  # one later call
    assert _silent(session, JUST_BEFORE_T2) is False  # the second was held but is not public yet
    assert _silent(session, T2_PUBLISHED) is True  # public: two clean calls, neither mentions it


def test_a_later_mention_breaks_silence_even_in_another_spelling(session, fy27_guidance):
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("revenue", "FY 2026-27", 5200, 6000))
    assert _silent(session, ist(2026, 1, 1)) is False


def test_a_later_call_with_quarantined_claims_cannot_establish_silence(session, fy27_guidance):
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("pat", "FY27", 500, 600), quarantined=1)
    assert _silent(session, ist(2026, 1, 1)) is False


def test_a_reextracted_call_is_judged_on_the_extraction_that_stands(session, fy27_guidance):
    # The first run of the second call mentions the claim; the run that stands does not.
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("revenue", "FY27", 5000, 6000), model="model-1")
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("pat", "FY27", 500, 600), model="model-2")
    assert _silent(session, ist(2026, 1, 1)) is True


def test_only_calls_after_the_claims_own_call_count(session):
    # An earlier call that omits the metric says nothing about a promise not yet made.
    _call(session, "earlier", date(2025, 2, 10), ist(2025, 2, 12), spec("pat", "FY26", 500, 600))
    _call(session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY27", 5000, 6000))
    _call(session, "q1-call", T1_CALL, T1_PUBLISHED, spec("pat", "FY27", 500, 600))

    assert _silent(session, ist(2025, 9, 1)) is False


def test_a_claim_whose_period_is_resolved_is_never_silent(session):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)
    _call(session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 4000, 5000))
    _call(session, "q1-call", T1_CALL, T1_PUBLISHED, spec("pat", "FY26", 500, 600))
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("pat", "FY26", 500, 600))

    assert _silent(session, BEFORE_RESULTS) is True  # the year had not closed: dropped
    assert _silent(session, AFTER_RESULTS) is False  # it has an answer now


# --------------------------------------------------------------------------- #
# Delivery rate
# --------------------------------------------------------------------------- #


def test_delivery_counts_only_graded_claims_and_reports_the_rest_apart(session):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4300, profit_loss_for_period=645)
    _call(
        session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED,
        spec("revenue", "FY26", 5000, 6000, hedge=HedgeStrength.WILL),  # 4300 is under 5000: missed, weight 4
        spec("pat_margin", "FY26", 15, unit=Unit.PERCENT, hedge=HedgeStrength.EXPECT),  # 645/4300: met, weight 3
        spec("revenue", "FY28", 7000, 8000),  # period not closed, and no later call mentions it: silent
        spec("roe", "FY26", 15, unit=Unit.PERCENT),  # not in the filings; also never mentioned again: silent
    )  # fmt: skip
    _call(session, "q1-call", T1_CALL, T1_PUBLISHED, spec("roe", "FY27", 15, unit=Unit.PERCENT))
    _call(session, "q2-call", T2_CALL, T2_PUBLISHED, spec("roe", "FY27", 15, unit=Unit.PERCENT))

    delivery = guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESULTS)

    assert delivery.rate == D(3) / D(7)  # 3 / (3 + 4)
    # roe FY27 is unresolvable and only one call follows it, so it is not silent.
    assert (delivery.met, delivery.missed, delivery.open, delivery.unresolvable, delivery.silent) == (1, 1, 0, 1, 2)


def test_delivery_grades_the_original_promise_unless_asked_for_the_last(session):
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4300)
    _call(session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 5000, 6000))
    # Management lowers the target mid-year, then meets it.
    _call(session, "q1-call", T1_CALL, T1_PUBLISHED, spec("revenue", "FY26", 4000, 4400))

    first = guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESULTS)
    last = guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESULTS, basis="last")
    both = _read(session, AFTER_RESULTS)

    assert (first.rate, first.met, first.missed) == (D(0), 0, 1)
    assert (last.rate, last.met, last.missed) == (D(1), 1, 0)
    assert [r.resolution.status for r in both] == [Status.MISSED, Status.MET]  # each is still resolvable


def test_delivery_is_read_at_t_and_changes_with_a_restatement(session):
    _call(session, "q4-call", CLAIM_CALL, CLAIM_PUBLISHED, spec("revenue", "FY26", 4000, 5000))
    _report(session, "fy26", RESULTS, FY26, revenue_from_operations=4500)
    _report(session, "fy26-restated", RESTATED, FY26, revenue_from_operations=3900)

    assert guidance_delivery_as_of(session, isin=RELIANCE, as_of=BEFORE_RESULTS).open == 1
    assert guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESULTS).rate == D(1)
    assert guidance_delivery_as_of(session, isin=RELIANCE, as_of=AFTER_RESTATEMENT).rate == D(0)


def test_an_isin_with_no_guidance_reads_empty(session):
    assert guidance_resolutions_as_of(session, isin="INE009A01021", as_of=AFTER_RESULTS) == []
    assert guidance_delivery_as_of(session, isin="INE009A01021", as_of=AFTER_RESULTS).rate is None
