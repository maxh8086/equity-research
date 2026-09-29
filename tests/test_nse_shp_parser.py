"""Contract tests: the shareholding-pattern parser against ten real NSE filings (tests/fixtures/nse_shp)."""

from datetime import date
from pathlib import Path

import pytest

from core.db.models import ShareholdingQuarantineReason as Reason
from ingest.nse_shp import mapping
from ingest.nse_shp.mapping import Layout
from ingest.nse_shp.parser import FileRejected, ParsedFiling, ParsedValue, parse_filing

FIXTURES = Path(__file__).parent / "fixtures" / "nse_shp"
HDFCBANK_Q2 = "SHP_1574385_13112025090903_WEB.xml"
INFY_ALLOTMENT = "SHP_1584868_11122025040253_WEB.xml"
INFY_2021 = "SHP_162513_538030_19102021032428_WEB.xml"
HDFCLIFE = "SHP_1660737_28042026112844_WEB.xml"
HDFCBANK_Q1 = "SHP_1687801_03072026023346_WEB.xml"
ADANIPORTS = "SHP_1690812_10072026084030_WEB.xml"
INFY_Q1 = "SHP_1693581_15072026065242_WEB.xml"
INDUSINDBK = "SHP_1695339_17072026045502_WEB.xml"
VEDL = "SHP_1696479_20072026120029_WEB.xml"
JSWSTEEL = "SHP_1698632_21072026062045_WEB.xml"

EXPECTED = {
    # file: (taxonomy, symbol, isin, scrip code, as-on date, allotment date, facts, typed deferred, percentages)
    HDFCBANK_Q2: ("2025-05-31", "HDFCBANK", "INE040A01034", "500180", date(2025, 9, 30), None, 397, 296, 108),
    INFY_ALLOTMENT: ("2025-05-31", "INFY", "INE009A01021", "500209", date(2025, 12, 4), date(2025, 12, 4),
                     432, 508, 114),
    INFY_2021: ("2020-09-30", "INFY", "INE009A01021", "500209", date(2021, 9, 30), None, 481, 760, 153),
    HDFCLIFE: ("2025-10-31", "HDFCLIFE", "INE795G01014", "540777", date(2026, 3, 31), None, 367, 305, 110),
    HDFCBANK_Q1: ("2025-10-31", "HDFCBANK", "INE040A01034", "500180", date(2026, 6, 30), None, 397, 356, 108),
    ADANIPORTS: ("2025-10-31", "ADANIPORTS", "INE742F01042", "532921", date(2026, 6, 30), None, 299, 368, 110),
    INFY_Q1: ("2025-10-31", "INFY", "INE009A01021", "500209", date(2026, 6, 30), None, 406, 504, 108),
    INDUSINDBK: ("2025-10-31", "INDUSINDBK", "INE095A01012", "532187", date(2026, 6, 30), None, 370, 606, 110),
    VEDL: ("2025-10-31", "VEDL", "INE205A01025", "500295", date(2026, 6, 30), None, 377, 599, 143),
    JSWSTEEL: ("2025-10-31", "JSWSTEEL", "INE019A01038", "500228", date(2026, 6, 30), None, 495, 954, 177),
}  # fmt: skip


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _parse(name: str) -> ParsedFiling:
    return parse_filing(_bytes(name))


def _values(parsed: ParsedFiling) -> dict[tuple[str, str], int]:
    return {(f.category, f.measure): f.value for f in parsed.facts}


def _rejected(data: bytes) -> FileRejected:
    with pytest.raises(FileRejected) as exc:
        parse_filing(data)
    return exc.value


def _replace(data: bytes, old: bytes, new: bytes) -> bytes:
    assert data.count(old) == 1, f"{old!r} occurs {data.count(old)} times"
    return data.replace(old, new)


VEDL_BODIES_CORPORATE_HOLDERS = b'<in-bse-shp:NumberOfShareholders contextRef="BodiesCorporate_ContextI" decimals="INF" unitRef="pure">5341<'  # noqa: E501


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_parses_to_its_recorded_shape(name):
    version, symbol, isin, scrip, as_on, allotment, n_facts, typed, percentages = EXPECTED[name]
    parsed = _parse(name)
    assert (parsed.taxonomy_version, parsed.layout) == (version, mapping.TAXONOMY_VERSIONS[version])
    assert (parsed.symbol, parsed.isin, parsed.scrip_code) == (symbol, isin, scrip)
    assert (parsed.as_on_date, parsed.allotment_date) == (as_on, allotment)
    assert (len(parsed.facts), parsed.typed_facts_deferred, parsed.percentage_facts_skipped) == (
        n_facts, typed, percentages,
    )  # fmt: skip
    assert parsed.issues == ()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_count_is_mapped_with_its_parent(name):
    parsed = _parse(name)
    categories = {c.key: c for c in mapping.CATEGORIES[parsed.layout].values()}
    measures = {f"in-bse-shp:{local}": m.key for local, m in mapping.MEASURES[parsed.layout].items()}
    members = {f"in-bse-shp:{member}": c.key for member, c in mapping.CATEGORIES[parsed.layout].items()}
    for f in parsed.facts:
        assert measures[f.xbrl_element] == f.measure
        assert members[f.xbrl_member] == f.category
        assert categories[f.category].parent == f.parent_category
        assert isinstance(f.value, int) and f.value >= 0
    assert len({(f.category, f.measure) for f in parsed.facts}) == len(parsed.facts)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_parent_is_the_sum_of_its_children(name):
    parsed = _parse(name)
    values = _values(parsed)
    children: dict[str, list[str]] = {}
    for c in mapping.CATEGORIES[parsed.layout].values():
        if c.parent is not None:
            children.setdefault(c.parent, []).append(c.key)
    for measure in {f.measure for f in parsed.facts}:
        for parent, kids in children.items():
            if (parent, measure) in values:
                assert values[(parent, measure)] == sum(values.get((k, measure), 0) for k in kids)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_total_is_promoter_plus_public_plus_non_public(name):
    values = _values(_parse(name))
    parts = ("promoter_group", "public", "non_promoter_non_public")
    assert values[("total", "total_shares")] == sum(values.get((p, "total_shares"), 0) for p in parts)


def test_counts_match_published_pattern():
    """INFY 30-Sep-2021: promoter group 55,16,82,338 of 4,20,54,64,426 shares (13.12%)."""
    values = _values(_parse(INFY_2021))
    assert values[("promoter_group", "total_shares")] == 551_682_338
    assert values[("total", "total_shares")] == 4_205_464_426
    assert values[("public", "total_shares")] == 3_638_941_503
    assert values[("promoter_group", "encumbered_shares")] == 0


def test_bank_with_no_promoter_reports_none():
    values = _values(_parse(HDFCBANK_Q1))
    assert ("promoter_group", "total_shares") not in values
    assert values[("public", "total_shares")] + values.get(("non_promoter_non_public", "total_shares"), 0) == (
        values[("total", "total_shares")]
    )


def test_pledged_and_other_encumbrances_are_separate_counts():
    """2025 layout: pledged, non-disposal undertakings and other encumbrances add up to the encumbered total."""
    jsw = _values(_parse(JSWSTEEL))
    assert jsw[("promoter_group", "pledged_shares")] == 125_650_440
    vedl = _values(_parse(VEDL))
    assert ("promoter_group", "pledged_shares") not in vedl
    assert vedl[("promoter_group", "ndu_encumbered_shares")] == 107_342_705
    assert vedl[("promoter_group", "other_encumbered_shares")] == 2_032_309_058
    assert vedl[("promoter_group", "encumbered_shares")] == 107_342_705 + 2_032_309_058


def test_layouts_keep_their_own_category_keys():
    """A 2020 `fpi` is not a 2025 `fpi_category_1`: categories are never merged across layouts."""
    old = {f.category for f in _parse(INFY_2021).facts}
    new = {f.category for f in _parse(JSWSTEEL).facts}
    assert {"fpi", "institutions"} <= old and not {"fpi_category_1", "institutions_foreign"} & old
    assert {"fpi_category_1", "fpi_category_2", "institutions_foreign", "institutions_domestic"} <= new
    assert not {"fpi", "institutions"} & new
    jsw = _values(_parse(JSWSTEEL))
    assert (jsw[("fpi_category_1", "total_shares")], jsw[("fpi_category_2", "total_shares")]) == (
        230_966_705, 35_856_896,
    )  # fmt: skip


def test_no_percentage_is_stored():
    for name in EXPECTED:
        assert not any("percent" in f.xbrl_element.lower() for f in _parse(name).facts)


def test_value_records_its_context():
    (fact,) = [f for f in _parse(INFY_2021).facts if (f.category, f.measure) == ("fpi", "total_shares")]
    assert fact == ParsedValue(
        "fpi", "institutions", "total_shares", 1_407_166_985, "in-bse-shp:NumberOfShares",
        "in-bse-shp:InstitutionsForeignPortfolioInvestorMember", ("InstitutionsForeignPortfolioInvestorI",),
    )  # fmt: skip


def test_layouts_are_mapped_for_every_supported_taxonomy():
    assert set(mapping.TAXONOMY_VERSIONS.values()) == set(Layout)
    for layout in Layout:
        keys = [c.key for c in mapping.CATEGORIES[layout].values()]
        assert len(keys) == len(set(keys))
        parents = {c.parent for c in mapping.CATEGORIES[layout].values()} - {None}
        assert parents <= set(keys)
        assert set(mapping.REQUIRED_CATEGORIES) <= set(keys)


# --------------------------------------------------------------------------- #
# Single facts held back, the rest of the file kept
# --------------------------------------------------------------------------- #


def test_unknown_category_is_quarantined_and_the_rest_kept():
    data = _replace(
        _bytes(VEDL),
        b">in-bse-shp:BodiesCorporateMember</xbrldi:explicitMember>",
        b">in-bse-shp:NewSebiCategoryMember</xbrldi:explicitMember>",
    )
    parsed = parse_filing(data)
    assert parsed.issues and {i.reason for i in parsed.issues} == {Reason.UNMAPPED_CATEGORY}
    assert {i.context_ref for i in parsed.issues} == {"BodiesCorporate_ContextI"}
    assert not any(f.category == "bodies_corporate" for f in parsed.facts)
    assert _values(parsed)[("total", "total_shares")] == _values(_parse(VEDL))[("total", "total_shares")]


def test_malformed_count_is_quarantined():
    data = _replace(_bytes(VEDL), VEDL_BODIES_CORPORATE_HOLDERS, VEDL_BODIES_CORPORATE_HOLDERS.replace(b">5341<", b">53.41<"))
    parsed = parse_filing(data)
    (issue,) = parsed.issues
    assert (issue.reason, issue.raw_value, issue.xbrl_element) == (
        Reason.MALFORMED_VALUE, "53.41", "in-bse-shp:NumberOfShareholders",
    )  # fmt: skip
    assert ("bodies_corporate", "shareholders") not in _values(parsed)
    assert ("bodies_corporate", "total_shares") in _values(parsed)


def test_count_with_a_decimal_precision_is_quarantined():
    old = VEDL_BODIES_CORPORATE_HOLDERS
    parsed = parse_filing(_replace(_bytes(VEDL), old, old.replace(b'decimals="INF"', b'decimals="0"')))
    assert [i.reason for i in parsed.issues] == [Reason.MALFORMED_VALUE]


def test_count_in_the_wrong_unit_is_quarantined():
    old = VEDL_BODIES_CORPORATE_HOLDERS
    parsed = parse_filing(_replace(_bytes(VEDL), old, old.replace(b'unitRef="pure"', b'unitRef="shares"')))
    assert [i.reason for i in parsed.issues] == [Reason.UNEXPECTED_UNIT]


def test_one_count_reported_twice_with_different_values_is_quarantined():
    old = VEDL_BODIES_CORPORATE_HOLDERS
    twice = old + b"/in-bse-shp:NumberOfShareholders>" + old.replace(b">5341<", b">5342<")
    parsed = parse_filing(_replace(_bytes(VEDL), old, twice))
    assert [i.reason for i in parsed.issues] == [Reason.CONFLICTING_VALUES] * 2
    assert ("bodies_corporate", "shareholders") not in _values(parsed)


def test_same_count_reported_twice_is_kept_once():
    old = VEDL_BODIES_CORPORATE_HOLDERS
    parsed = parse_filing(_replace(_bytes(VEDL), old, old + b"/in-bse-shp:NumberOfShareholders>" + old))
    assert parsed.issues == ()
    assert _values(parsed)[("bodies_corporate", "shareholders")] == 5341


def test_unknown_numeric_fact_without_a_category_is_quarantined():
    extra = b'<in-bse-shp:NumberOfSharesSomethingNew contextRef="MainI" decimals="INF" unitRef="shares">5</in-bse-shp:NumberOfSharesSomethingNew>'  # noqa: E501
    data = _bytes(VEDL).replace(b"</xbrli:xbrl>", extra + b"</xbrli:xbrl>")
    (issue,) = parse_filing(data).issues
    assert (issue.reason, issue.context_ref) == (Reason.UNMAPPED_ELEMENT, "MainI")


# --------------------------------------------------------------------------- #
# Whole file rejected
# --------------------------------------------------------------------------- #


def test_parent_that_is_not_the_sum_of_its_children_rejects_the_file():
    old = b'<in-bse-shp:NumberOfShares contextRef="Indian_ContextI" decimals="INF" unitRef="shares">142996<'
    exc = _rejected(_replace(_bytes(VEDL), old, old.replace(b">142996<", b">142997<")))
    assert exc.reason is Reason.TOTALS_MISMATCH


def test_unsupported_taxonomy_is_rejected():
    exc = _rejected(_bytes(VEDL).replace(b"in-bse-shp-2025-10-31.xsd", b"in-bse-shp-2018-01-31.xsd"))
    assert exc.reason is Reason.UNSUPPORTED_TAXONOMY


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: b'<!DOCTYPE x [<!ENTITY e "e">]>' + d,
        lambda d: d[: len(d) // 2],
        lambda d: _replace(d, b'<in-bse-shp:DateOfReport contextRef="MainI">2026-06-30<',
                           b'<in-bse-shp:DateOfReport contextRef="MainI">2026-03-31<'),
        lambda d: d.replace(b">500295</xbrli:identifier>", b">500296</xbrli:identifier>", 1),
        lambda d: d.replace(b'contextRef="BodiesCorporate_ContextI"', b'contextRef="Nowhere_ContextI"', 1),
    ],
    ids=["doctype", "truncated", "as-on-date-differs", "identifier-differs", "unknown-context"],
)  # fmt: skip
def test_changed_shape_rejects_the_file(mutate):
    assert _rejected(mutate(_bytes(VEDL))).reason is Reason.SHAPE_CHANGED


def test_missing_public_total_rejects_the_file():
    data = _bytes(VEDL)
    old = b">in-bse-shp:PublicShareholdingMember</xbrldi:explicitMember>"
    exc = _rejected(_replace(data, old, b">in-bse-shp:NewSebiCategoryMember</xbrldi:explicitMember>"))
    assert exc.reason is Reason.SHAPE_CHANGED and "public" in exc.detail
