"""Contract tests: the XBRL results parser against eight real NSE filings (tests/fixtures/nse_xbrl)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from core.db.models import Consolidation, XbrlTaxonomy
from core.db.models import FinancialFactsQuarantineReason as Reason
from ingest.nse_xbrl import mapping
from ingest.nse_xbrl.parser import FileRejected, ParsedFiling, parse_filing

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "nse_xbrl"
INFY_Q4 = "INDAS_104589_1099938_19042024112830.xml"
INFY_Q2 = "INDAS_112850_1276017_17102024074402.xml"
TATASTEEL = "INDAS_114281_1299108_06112024065731.xml"
BAJFINANCE = "NBFC_INDAS_113038_1285327_22102024074026.xml"
HDFCBANK_Q2 = "BANKING_112946_1281172_20102024121424.xml"
HDFCBANK_Q3 = "BANKING_117524_1359008_23012025122553.xml"
HDFCLIFE = "LI_112794_1271828_16102024124453.xml"
ICICIGI = "GI_117338_1350247_17012025074725.xml"

EXPECTED = {
    # file: (taxonomy, symbol, isin, consolidation, period_start, period_end, board meeting, facts, issues, deferred)
    INFY_Q4: (XbrlTaxonomy.IND_AS, "INFY", None, Consolidation.CONSOLIDATED,
              date(2024, 1, 1), date(2024, 3, 31), date(2024, 4, 18), 245, 2, 70),
    INFY_Q2: (XbrlTaxonomy.IND_AS, "INFY", None, Consolidation.STANDALONE,
              date(2024, 7, 1), date(2024, 9, 30), date(2024, 10, 17), 225, 2, 20),
    TATASTEEL: (XbrlTaxonomy.IND_AS, "TATASTEEL", None, Consolidation.CONSOLIDATED,
                date(2024, 7, 1), date(2024, 9, 30), date(2024, 11, 6), 254, 2, 54),
    BAJFINANCE: (XbrlTaxonomy.NBFC, "BAJFINANCE", None, Consolidation.STANDALONE,
                 date(2024, 7, 1), date(2024, 9, 30), date(2024, 10, 22), 236, 2, 8),
    HDFCBANK_Q2: (XbrlTaxonomy.BANK, "HDFCBANK", "INE040A01034", Consolidation.CONSOLIDATED,
                  date(2024, 7, 1), date(2024, 9, 30), date(2024, 10, 19), 168, 2, 92),
    HDFCBANK_Q3: (XbrlTaxonomy.BANK, "HDFCBANK", "INE040A01034", Consolidation.STANDALONE,
                  date(2024, 10, 1), date(2024, 12, 31), date(2025, 1, 22), 82, 0, 52),
    HDFCLIFE: (XbrlTaxonomy.LIFE_INSURANCE, "HDFCLIFE", "INE795G01014", Consolidation.STANDALONE,
               date(2024, 7, 1), date(2024, 9, 30), date(2024, 10, 15), 313, 2, 174),
    ICICIGI: (XbrlTaxonomy.GENERAL_INSURANCE, "ICICIGI", "INE765G01017", Consolidation.STANDALONE,
              date(2024, 10, 1), date(2024, 12, 31), date(2025, 1, 17), 122, 2, 154),
}  # fmt: skip


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _parse(name: str) -> ParsedFiling:
    return parse_filing(_bytes(name))


def _facts(parsed: ParsedFiling, line_item: str) -> dict[tuple[date | None, date], Decimal]:
    return {(f.period_start, f.period_end): f.value for f in parsed.facts if f.line_item == line_item}


def _rejected(data: bytes) -> FileRejected:
    with pytest.raises(FileRejected) as exc:
        parse_filing(data)
    return exc.value


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_parses_to_its_recorded_shape(name):
    taxonomy, symbol, isin, consolidation, start, end, meeting, n_facts, n_issues, deferred = EXPECTED[name]
    parsed = _parse(name)
    assert parsed.taxonomy.family is taxonomy
    assert (parsed.symbol, parsed.isin, parsed.consolidation) == (symbol, isin, consolidation)
    assert (parsed.period_start, parsed.period_end, parsed.board_meeting_date) == (start, end, meeting)
    assert (len(parsed.facts), len(parsed.issues), parsed.dimensional_facts_deferred) == (n_facts, n_issues, deferred)
    assert {i.reason for i in parsed.issues} <= {Reason.CONFLICTING_VALUES}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_fact_is_mapped_typed_and_within_the_filing(name):
    parsed = _parse(name)
    known = mapping.LINE_ITEMS[parsed.taxonomy.namespace]
    for f in parsed.facts:
        prefix, _, local = f.xbrl_element.partition(":")
        assert prefix == parsed.taxonomy.prefix
        assert known[local] == (f.line_item, f.unit)
        assert isinstance(f.value, Decimal)
        assert f.period_end == parsed.period_end
        assert f.period_start is None or f.period_start <= f.period_end


def test_quarter_and_year_to_date_columns_match_published_results():
    """INFY FY24: revenue Rs 1,53,670 cr for the year, Rs 37,923 cr for Q4."""
    parsed = _parse(INFY_Q4)
    assert _facts(parsed, "revenue_from_operations") == {
        (date(2024, 1, 1), date(2024, 3, 31)): D("379230000000.00"),
        (date(2023, 4, 1), date(2024, 3, 31)): D("1536700000000.00"),
    }
    eps = _facts(parsed, "basic_earnings_loss_per_share_from_continuing_and_discontinued_operations")
    assert eps[(date(2024, 1, 1), date(2024, 3, 31))] == D("19.25")


def test_bank_profit_matches_published_results():
    """HDFCBANK Q3 FY25 standalone net profit: Rs 16,735.50 cr."""
    profit = _facts(_parse(HDFCBANK_Q3), "profit_loss_for_the_period")
    assert profit[(date(2024, 10, 1), date(2024, 12, 31))] == D("167355000000.00")


def test_instant_facts_have_no_start_and_merge_across_columns():
    parsed = _parse(INFY_Q4)
    cwip = [f for f in parsed.facts if f.line_item == "capital_work_in_progress"]
    assert len(cwip) == 1
    assert (cwip[0].period_start, cwip[0].period_end, cwip[0].value) == (None, date(2024, 3, 31), D("2930000000.00"))


def test_units_are_resolved_from_measures():
    parsed = _parse(HDFCBANK_Q3)
    units = {f.line_item: f.unit for f in parsed.facts}
    assert units["interest_earned"] == "INR"
    assert units["cet1_ratio"] == "pure"
    eps = {f.unit for f in _parse(INFY_Q2).facts if f.line_item.startswith("basic_earnings")}
    assert eps == {"INR/share"}


def test_a_double_tagged_element_is_quarantined_whole_never_picked():
    parsed = _parse(INFY_Q2)
    conflicts = [i for i in parsed.issues if i.reason is Reason.CONFLICTING_VALUES]
    assert {i.xbrl_element for i in conflicts} == {"in-bse-fin-2020:CashAndCashEquivalentsCashFlowStatement"}
    assert {i.raw_value for i in conflicts} == {"81910000000.00", "139170000000.00"}
    assert not _facts(parsed, "cash_and_cash_equivalents_cash_flow_statement")


def test_identical_duplicates_are_merged_with_every_context():
    data = _bytes(INFY_Q2).replace(
        b"</xbrli:xbrl>",
        b'<in-bse-fin:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-7">'
        b"342570000000.00</in-bse-fin:RevenueFromOperations></xbrli:xbrl>",
    )
    parsed = parse_filing(data)
    revenue = [f for f in parsed.facts if f.line_item == "revenue_from_operations" and f.period_start.month == 7]
    assert len(revenue) == 1 and revenue[0].value == D("342570000000.00")
    assert len(parsed.issues) == len(_parse(INFY_Q2).issues)


# --------------------------------------------------------------------------- #
# Single-fact problems: quarantined, the rest of the file kept
# --------------------------------------------------------------------------- #


def _with_fact(fact: bytes) -> ParsedFiling:
    return parse_filing(_bytes(INFY_Q2).replace(b"</xbrli:xbrl>", fact + b"</xbrli:xbrl>"))


def test_unmapped_element_is_quarantined_not_guessed():
    parsed = _with_fact(b'<in-bse-fin:MadeUpElement contextRef="OneD" unitRef="INR" decimals="-7">1.00'
                        b"</in-bse-fin:MadeUpElement>")  # fmt: skip
    (issue,) = [i for i in parsed.issues if i.reason is Reason.UNMAPPED_ELEMENT]
    assert (issue.xbrl_element, issue.context_ref, issue.raw_value) == ("in-bse-fin-2020:MadeUpElement", "OneD", "1.00")
    assert len(parsed.facts) == EXPECTED[INFY_Q2][7]


def test_element_from_another_namespace_is_unmapped():
    parsed = _with_fact(b'<x:RevenueFromOperations xmlns:x="http://example.org/other" contextRef="OneD" '
                        b'unitRef="INR" decimals="-7">1.00</x:RevenueFromOperations>')  # fmt: skip
    assert [i.reason for i in parsed.issues if "example.org" in i.xbrl_element] == [Reason.UNMAPPED_ELEMENT]


def test_wrong_unit_is_quarantined():
    parsed = _with_fact(b'<in-bse-fin:OtherIncome contextRef="OneD" unitRef="shares" decimals="0">5'
                        b"</in-bse-fin:OtherIncome>")  # fmt: skip
    assert Reason.UNEXPECTED_UNIT in {i.reason for i in parsed.issues}


@pytest.mark.parametrize(
    ("value", "decimals", "reason"),
    [
        (b"1,000.00", b"-7", Reason.MALFORMED_VALUE),
        (b"1e9", b"-7", Reason.MALFORMED_VALUE),
        (b"1.0000001", b"INF", Reason.UNEXPECTED_SCALE),
        (b"1" * 23, b"0", Reason.UNEXPECTED_SCALE),
        (b"1.00", b"lakhs", Reason.UNEXPECTED_SCALE),
    ],
)
def test_bad_values_are_quarantined(value, decimals, reason):
    parsed = _with_fact(b'<in-bse-fin:FinanceCosts contextRef="OneD" unitRef="INR" decimals="' + decimals + b'">'
                        + value + b"</in-bse-fin:FinanceCosts>")  # fmt: skip
    assert reason in {i.reason for i in parsed.issues if i.xbrl_element.endswith("FinanceCosts")}


def test_nil_facts_are_skipped():
    parsed = _with_fact(b'<in-bse-fin:MadeUpElement xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                        b'xsi:nil="true" contextRef="OneD" unitRef="INR" decimals="-7"/>')  # fmt: skip
    assert len(parsed.issues) == EXPECTED[INFY_Q2][8]


# --------------------------------------------------------------------------- #
# Whole-file rejections
# --------------------------------------------------------------------------- #


def test_doctype_is_rejected():
    data = _bytes(INFY_Q2).replace(b"?>", b'?><!DOCTYPE x [<!ENTITY a "b">]>', 1)
    assert _rejected(data).reason is Reason.SHAPE_CHANGED


def test_malformed_xml_is_rejected():
    assert _rejected(_bytes(INFY_Q2)[:-40]).reason is Reason.SHAPE_CHANGED


def test_unknown_schema_ref_is_unsupported():
    data = _bytes(INFY_Q2).replace(b"Ind-AS_entry_point_2020-03-31.xsd", b"Ind-AS_entry_point_2016-03-31.xsd")
    assert _rejected(data).reason is Reason.UNSUPPORTED_TAXONOMY


def test_life_and_general_insurance_are_told_apart_by_entry_namespace():
    data = _bytes(HDFCLIFE).replace(b"xbrl/Insurance/2020-03-31", b"xbrl/Unknown/2020-03-31")
    assert _rejected(data).reason is Reason.UNSUPPORTED_TAXONOMY


def test_missing_reporting_period_is_unconfirmed():
    data = _bytes(INFY_Q2).replace(
        b'<in-bse-fin:DateOfEndOfReportingPeriod contextRef="OneD">2024-09-30</in-bse-fin:DateOfEndOfReportingPeriod>',
        b"",
    )
    assert _rejected(data).reason is Reason.PERIOD_UNCONFIRMED


def test_context_ending_elsewhere_than_its_column_is_unconfirmed():
    data = _bytes(INFY_Q2).replace(
        b'<in-bse-fin:DateOfEndOfReportingPeriod contextRef="FourD">2024-09-30<',
        b'<in-bse-fin:DateOfEndOfReportingPeriod contextRef="FourD">2024-06-30<',
    )
    assert _rejected(data).reason is Reason.PERIOD_UNCONFIRMED


def test_foreign_presentation_currency_is_rejected():
    data = _bytes(INFY_Q2).replace(
        b'<in-bse-fin:DescriptionOfPresentationCurrency contextRef="OneD">INR<',
        b'<in-bse-fin:DescriptionOfPresentationCurrency contextRef="OneD">USD<',
    )
    assert _rejected(data).reason is Reason.SHAPE_CHANGED


def test_unexpected_context_id_is_rejected():
    data = _bytes(INFY_Q2).replace(b'id="OneD"', b'id="SevenD"').replace(b'contextRef="OneD"', b'contextRef="SevenD"')
    assert _rejected(data).reason is Reason.SHAPE_CHANGED


def test_two_symbols_are_rejected():
    data = _bytes(INFY_Q2).replace(b">INFY</xbrli:identifier>", b">TCS</xbrli:identifier>", 1)
    assert _rejected(data).reason is Reason.SHAPE_CHANGED


def test_parse_is_deterministic():
    assert _parse(TATASTEEL) == _parse(TATASTEEL)
