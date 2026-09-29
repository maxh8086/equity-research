from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.compute.order_terms import (
    AWARD_PHRASES,
    BOILERPLATE_PHRASES,
    EXCLUSION_PHRASES,
    PRE_AWARD_PHRASES,
    UNIT_MULTIPLIERS,
    Verdict,
    classify,
    parse_amounts,
    search_query,
)

# Announcement wording follows the shape NSE/BSE Regulation 30 filings use.
REG30 = (
    "Pursuant to Regulation 30 of SEBI (Listing Obligations and Disclosure "
    "Requirements) Regulations, 2015, we wish to inform you that "
)


def reg30(body: str) -> str:
    return REG30 + body


# --- the boilerplate trap ---------------------------------------------------


def test_the_lodr_boilerplate_every_filing_carries_does_not_exclude_it():
    # If this fails, the filter throws away the entire corpus: nearly every
    # NSE/BSE announcement cites SEBI (LODR) Regulations, 2015.
    c = classify(reg30("the Company has received an order for supply of 40 locomotives."))
    assert c.verdict is Verdict.AWARD
    assert c.exclusions == ()


def test_boilerplate_alone_matches_nothing():
    assert classify(REG30).verdict is Verdict.NO_MATCH


@pytest.mark.parametrize("phrase", BOILERPLATE_PHRASES)
def test_no_boilerplate_phrase_is_also_an_exclusion_phrase(phrase):
    assert phrase not in EXCLUSION_PHRASES


# --- awards -----------------------------------------------------------------


def test_a_plain_contract_award_is_an_award():
    c = classify(reg30("the Company has received a Letter of Award from NHAI."))
    assert c.verdict is Verdict.AWARD
    assert [m.phrase for m in c.awards] == ["letter of award"]
    assert c.is_lead


def test_the_longest_matching_phrase_is_the_one_reported():
    c = classify("Notification of Award received from the client.")
    assert [m.phrase for m in c.awards] == ["notification of award"]


def test_a_match_carries_a_quote_a_person_can_check():
    c = classify(reg30("the Company has bagged an order worth Rs. 1,250 crore from BHEL."))
    (m,) = [x for x in c.awards if x.phrase == "bagged an order"]
    assert "bagged an order" in m.quote.lower()
    assert m.quote in reg30("the Company has bagged an order worth Rs. 1,250 crore from BHEL.")


def test_matching_is_case_insensitive_and_reports_offsets_into_the_original():
    text = "LETTER OF INTENT signed today."
    (m,) = classify(text).awards
    assert (m.phrase, m.start, m.end) == ("letter of intent", 0, 16)
    assert text[m.start : m.end] == "LETTER OF INTENT"


def test_a_phrase_inside_a_longer_word_does_not_match():
    assert classify("Reorder levels were revised.").verdict is Verdict.NO_MATCH
    assert classify("The subcontract awarded internally.").awards == ()


# --- exclusions: the reason the query needs negatives at all -----------------


@pytest.mark.parametrize(
    "body",
    [
        "an assessment order under Section 143(3) of the Income Tax Act has been received.",
        "the Company has received a demand order from the Commissioner of GST.",
        "an order received from the Adjudicating Authority imposing a penalty.",
    ],
)
def test_a_tax_or_regulatory_order_is_not_an_order_win(body):
    assert classify(reg30(body)).verdict in (Verdict.EXCLUDED, Verdict.AMBIGUOUS)


def test_a_tax_order_with_no_award_wording_is_excluded_outright():
    c = classify(reg30("an assessment order was passed by the Assessing Officer."))
    assert c.verdict is Verdict.EXCLUDED
    assert not c.is_lead


def test_both_kinds_in_one_filing_is_ambiguous_and_never_guessed():
    c = classify(
        reg30(
            "the Company has received a Letter of Award from NHAI, and separately "
            "an assessment order from the Commissioner."
        )
    )
    assert c.verdict is Verdict.AMBIGUOUS
    assert c.awards and c.exclusions
    assert not c.is_lead


# --- pre-award is a weaker, separate class ----------------------------------


def test_l1_is_pre_award_not_an_order_win():
    c = classify(reg30("the Company has emerged as L1 bidder for the Mumbai package."))
    assert c.verdict is Verdict.PRE_AWARD
    assert c.awards == ()
    assert c.is_lead  # worth watching, but it is not a win


def test_l1_followed_by_an_actual_award_is_an_award():
    c = classify("Having emerged as L1 bidder, the Company has now received a Letter of Award.")
    assert c.verdict is Verdict.AWARD
    assert c.pre_awards and c.awards


def test_no_award_phrase_is_also_a_pre_award_phrase():
    assert not set(AWARD_PHRASES) & set(PRE_AWARD_PHRASES)


# --- amounts: parsed by code, quoted for checking ---------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("worth Rs. 1,250 crore", Decimal("12500000000")),
        ("valued at Rs 500 lakhs", Decimal("50000000")),
        ("₹12.5 crore", Decimal("125000000")),
        ("INR 2.4 billion", Decimal("2400000000")),
        ("aggregating to 1,00,000 crore Rs", Decimal("1000000000000")),
    ],
)
def test_indian_money_formats_parse_to_rupees_as_decimal(text, expected):
    (a,) = parse_amounts(text)
    assert a.inr == expected
    assert isinstance(a.inr, Decimal)
    assert a.quote in text


def test_every_amount_keeps_the_span_it_came_from():
    text = "Order of Rs. 300 crore and a second of Rs. 50 crore."
    amounts = parse_amounts(text)
    assert [a.inr for a in amounts] == [Decimal("3000000000"), Decimal("500000000")]
    for a in amounts:
        assert text[a.start : a.end] == a.quote


def test_a_bare_number_with_no_currency_is_not_an_amount():
    assert parse_amounts("40 locomotives over 3 years") == ()


def test_amounts_are_found_on_a_classified_filing():
    c = classify(reg30("the Company bagged an order worth Rs. 1,250 crore."))
    assert [a.inr for a in c.amounts] == [Decimal("12500000000")]


# --- the search query -------------------------------------------------------


def test_the_disjunction_is_parenthesised_and_the_negatives_sit_outside_it():
    q = search_query()
    assert q.count("(") == q.count(")") == 1
    body = q[q.index("(") :]
    assert " or " in body
    assert "-" not in body  # every negative is outside the group
    assert q.index('-"') < q.index("(")


def test_the_query_negates_no_boilerplate_term():
    q = search_query()
    for phrase in BOILERPLATE_PHRASES:
        assert f'-"{phrase}"' not in q


def test_no_quoted_phrase_has_stray_whitespace():
    # "bagged an order " with a trailing space inside the quotes matches nothing.
    import re

    for quoted in re.findall(r'"([^"]*)"', search_query(include_pre_award=True)):
        assert quoted == quoted.strip()
        assert "  " not in quoted


def test_pre_award_phrases_are_opt_in():
    assert '"l1 bidder"' not in search_query()
    assert '"l1 bidder"' in search_query(include_pre_award=True)


def test_every_award_phrase_reaches_the_query():
    q = search_query()
    for phrase in AWARD_PHRASES:
        assert f'"{phrase}"' in q


# --- properties -------------------------------------------------------------


@given(st.sampled_from(AWARD_PHRASES), st.sampled_from(EXCLUSION_PHRASES))
def test_an_award_phrase_beside_an_exclusion_is_always_ambiguous(award, exclusion):
    assert classify(f"{REG30} {award} and also {exclusion}.").verdict is Verdict.AMBIGUOUS


@given(st.sampled_from(AWARD_PHRASES))
def test_every_award_phrase_classifies_as_an_award_on_its_own(award):
    assert classify(reg30(f"the Company confirms a {award} today.")).verdict is Verdict.AWARD


@given(
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999"), places=2),
    st.sampled_from(sorted(UNIT_MULTIPLIERS)),
)
def test_a_parsed_amount_is_the_number_times_its_unit(number, unit):
    (a,) = parse_amounts(f"Rs. {number} {unit}")
    assert a.inr == number * UNIT_MULTIPLIERS[unit]


# --- order-like wording that is not a customer order ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The Board approved signing a Letter of Intent for acquisition of a 51% stake in XYZ Pvt Ltd.",
        "Details of related party transactions: purchase order placed with the subsidiary.",
        "Under the scheme of arrangement, a work order was novated to the resulting company.",
    ],
)
def test_an_acquisition_or_related_party_context_is_not_an_order_win(text):
    c = classify(text)
    assert c.verdict is Verdict.AMBIGUOUS
    assert c.exclusions
    assert not c.is_lead


def test_context_exclusions_are_kept_apart_from_tax_exclusions():
    from core.compute.order_terms import NON_ORDER_CONTEXT_PHRASES

    assert not set(NON_ORDER_CONTEXT_PHRASES) & set(EXCLUSION_PHRASES)


def test_an_ordinary_award_is_untouched_by_the_context_list():
    c = classify(reg30("the Company received a Letter of Award from Indian Railways."))
    assert c.verdict is Verdict.AWARD
