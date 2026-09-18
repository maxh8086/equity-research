"""Hardcoded mapping for SEBI shareholding-pattern XBRL (Regulation 31).

Research decision, not config (CLAUDE.md Code conventions). A category member
or element absent from these tables is quarantined, never guessed, and review
of the quarantine grows them under a new RULE_VERSION.

Two layouts exist. The 2020 taxonomy splits the public into institutions,
governments and non-institutions; the 2025 taxonomies (2025-05-31 and
2025-10-31, same element and member names) split institutions into domestic
and foreign and divide FPIs into categories I and II. A category keeps its own
meaning: where the layouts differ the keys differ (2020 `fpi` is not 2025
`fpi_category_1`, 2020 `individuals_up_to_2_lakh` includes non-residents,
2025 `resident_individuals_up_to_2_lakh` does not). Only members seen in each
layout's recorded files are mapped (tests/fixtures/nse_shp).

`CATEGORIES` also records each category's parent. Every parent equals the sum
of its children, for every stored measure (an absent child counts as zero);
the parser checks this and rejects a file that fails it. The tree was
confirmed by 1,197 such checks over 12 filings, and by document order where
members held zero.

Percentages are not stored: code recomputes them from the counts.
"""

from dataclasses import dataclass
from enum import StrEnum

RULE_VERSION = "nse-shp-1"


class Layout(StrEnum):
    LAYOUT_2020 = "2020"
    LAYOUT_2025 = "2025"


# taxonomy version (schemaRef in-bse-shp-<version>.xsd) -> layout
TAXONOMY_VERSIONS: dict[str, Layout] = {
    "2020-09-30": Layout.LAYOUT_2020,
    "2025-05-31": Layout.LAYOUT_2025,
    "2025-10-31": Layout.LAYOUT_2025,
}

SCHEMA_REF = "in-bse-shp-{version}.xsd"
NAMESPACE = "http://www.bseindia.com/xbrl/shp/{version}/in-bse-shp"
CATEGORY_AXIS = "CategoryOfShareholdersAxis"

META_DATE_OF_REPORT = "DateOfReport"
META_DATE_OF_ALLOTMENT = "DateOfAllotment"
META_ISIN = "ISIN"
META_SYMBOL = "Symbol"
META_SCRIP_CODE = "ScripCode"

# The 2020 filings report their identity facts under context ids that no
# context defines. Tolerated for non-numeric facts only, in that layout only.
UNDEFINED_IDENTITY_CONTEXTS: dict[Layout, frozenset[str]] = {
    Layout.LAYOUT_2020: frozenset({"OneD", "OneI"}),
    Layout.LAYOUT_2025: frozenset(),
}

# Totals and public row every file must report `total_shares` for.
REQUIRED_CATEGORIES = ("total", "public")


@dataclass(frozen=True)
class Category:
    key: str
    parent: str | None


def _tree(rows: tuple[tuple[str, str, str | None], ...]) -> dict[str, Category]:
    return {member: Category(key, parent) for member, key, parent in rows}


_PROMOTER_COMMON = (
    ("ShareholdingPatternMember", "total", None),
    ("ShareholdingOfPromoterAndPromoterGroupMember", "promoter_group", "total"),
    ("IndianMember", "promoter_indian", "promoter_group"),
    ("IndividualsOrHinduUndividedFamilyMember", "promoter_individuals_huf", "promoter_indian"),
    ("CentralGovernmentOrStateGovernmentSMember", "promoter_central_or_state_government", "promoter_indian"),
    ("IndianFinancialInstitutionsOrBanksMember", "promoter_financial_institutions_or_banks", "promoter_indian"),
    ("OtherIndianShareholdersMember", "promoter_other_indian", "promoter_indian"),
    ("ForeignMember", "promoter_foreign", "promoter_group"),
    ("NonResidentIndividualsOrForeignIndividualsMember", "promoter_nri_or_foreign_individuals", "promoter_foreign"),
    ("OtherForeignShareholdersMember", "promoter_other_foreign", "promoter_foreign"),
    ("PublicShareholdingMember", "public", "total"),
    ("SharesHeldByNonPromoterNonPublicShareholdersMember", "non_promoter_non_public", "total"),
    ("CustodianOrDRHolderMember", "custodian_dr_holder", "non_promoter_non_public"),
    ("EmployeeBenefitsTrustsMember", "employee_benefit_trusts", "non_promoter_non_public"),
)

CATEGORIES: dict[Layout, dict[str, Category]] = {
    Layout.LAYOUT_2020: _tree(
        _PROMOTER_COMMON
        + (
            ("ForeignGovernmentMember", "promoter_foreign_government", "promoter_foreign"),
            ("ForeignInstitutionsMember", "promoter_foreign_institutions", "promoter_foreign"),
            ("ForeignPortfolioInvestorMember", "promoter_foreign_portfolio_investors", "promoter_foreign"),
            ("InstitutionsMember", "institutions", "public"),
            ("MutualFundsOrUtiMember", "mutual_funds", "institutions"),
            ("VentureCapitalFundsMember", "venture_capital_funds", "institutions"),
            ("AlternativeInvestmentFundsMember", "alternative_investment_funds", "institutions"),
            ("ForeignVentureCapitalInvestorsMember", "foreign_venture_capital_investors", "institutions"),
            ("InstitutionsForeignPortfolioInvestorMember", "fpi", "institutions"),
            ("FinancialInstitutionOrBanksMember", "financial_institutions_or_banks", "institutions"),
            ("InsuranceCompaniesMember", "insurance_companies", "institutions"),
            ("ProvidentFundsOrPensionFundsMember", "provident_or_pension_funds", "institutions"),
            ("OtherInstitutionsMember", "other_institutions", "institutions"),
            ("GovermentsMember", "governments", "public"),  # sic: the taxonomy's spelling
            ("CentralGovernmentOrStateGovernmentSOrPresidentOfIndiaMember", "central_or_state_government",
             "governments"),  # fmt: skip
            ("NonInstitutionsMember", "non_institutions", "public"),
            ("IndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember", "individuals_up_to_2_lakh",
             "non_institutions"),  # fmt: skip
            ("IndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakhMember",
             "individuals_above_2_lakh", "non_institutions"),  # fmt: skip
            ("NBFCsRegisteredWithRbiMember", "nbfcs", "non_institutions"),
            ("EmployeeTrustsMember", "employee_trusts", "non_institutions"),
            ("OverseasDepositoriesMember", "overseas_depositories", "non_institutions"),
            ("OtherNonInstitutionsMember", "other_non_institutions", "non_institutions"),
        )
    ),
    Layout.LAYOUT_2025: _tree(
        _PROMOTER_COMMON
        + (
            ("InstitutionsDomesticMember", "institutions_domestic", "public"),
            ("MutualFundsOrUTIMember", "mutual_funds", "institutions_domestic"),
            ("VentureCapitalFundsMember", "venture_capital_funds", "institutions_domestic"),
            ("AlternativeInvestmentFundsMember", "alternative_investment_funds", "institutions_domestic"),
            ("BanksMember", "banks", "institutions_domestic"),
            ("InsuranceCompaniesMember", "insurance_companies", "institutions_domestic"),
            ("ProvidentFundsOrPensionFundsMember", "provident_or_pension_funds", "institutions_domestic"),
            ("AssetReconstructionCompaniesMember", "asset_reconstruction_companies", "institutions_domestic"),
            ("SovereignWealthFundsDomesticMember", "sovereign_wealth_funds_domestic", "institutions_domestic"),
            ("NBFCsRegisteredWithRBIMember", "nbfcs", "institutions_domestic"),
            ("OtherFinancialInstitutionsMember", "other_financial_institutions", "institutions_domestic"),
            ("OtherInstitutionsDomesticMember", "other_institutions_domestic", "institutions_domestic"),
            ("InstitutionsForeignMember", "institutions_foreign", "public"),
            ("ForeignDirectInvestmentMember", "foreign_direct_investment", "institutions_foreign"),
            ("ForeignVentureCapitalInvestorsMember", "foreign_venture_capital_investors", "institutions_foreign"),
            ("SovereignWealthFundsForeignMember", "sovereign_wealth_funds_foreign", "institutions_foreign"),
            ("InstitutionsForeignPortfolioInvestorCategoryOneMember", "fpi_category_1", "institutions_foreign"),
            ("InstitutionsForeignPortfolioInvestorCategoryTwoMember", "fpi_category_2", "institutions_foreign"),
            ("OverseasDepositoriesMember", "overseas_depositories", "institutions_foreign"),
            ("OtherInstitutionsForeignMember", "other_institutions_foreign", "institutions_foreign"),
            ("GovernmentsMember", "governments", "public"),
            ("CentralGovernmentOrPresidentOfIndiaMember", "central_government", "governments"),
            ("StateGovernmentsOrGovernorsMember", "state_governments", "governments"),
            ("ShareholdingByCompaniesOrBodiesCorporateWhereCentralOrStateGovernmentIsPromoterMember",
             "government_promoted_companies", "governments"),  # fmt: skip
            ("NonInstitutionsMember", "non_institutions", "public"),
            ("AssociateCompaniesOrSubsidiariesMember", "associate_companies_or_subsidiaries", "non_institutions"),
            ("DirectorsAndDirectorsRelativesMember", "directors_and_relatives", "non_institutions"),
            ("KeyManagerialPersonnelMember", "key_managerial_personnel", "non_institutions"),
            ("RelativesOfPromotersOtherThanPromoterGroupMember", "promoter_relatives_outside_group",
             "non_institutions"),  # fmt: skip
            ("TrustsWhereAnyPersonBelongingToPromoterAndPromoterGroupIsTrusteeOrBeneficiaryOrAuthorOfTrustMember",
             "promoter_linked_trusts", "non_institutions"),  # fmt: skip
            ("InvestorEducationAndProtectionFundMember", "iepf", "non_institutions"),
            ("ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember",
             "resident_individuals_up_to_2_lakh", "non_institutions"),  # fmt: skip
            ("ResidentIndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakhMember",
             "resident_individuals_above_2_lakh", "non_institutions"),  # fmt: skip
            ("NonResidentIndiansMember", "non_resident_indians", "non_institutions"),
            ("ForeignNationalsMember", "foreign_nationals", "non_institutions"),
            ("ForeignCompaniesMember", "foreign_companies", "non_institutions"),
            ("BodiesCorporateMember", "bodies_corporate", "non_institutions"),
            ("OtherNonInstitutionsMember", "other_non_institutions", "non_institutions"),
        )
    ),
}


@dataclass(frozen=True)
class Measure:
    key: str
    unit: str  # "shares" or "pure" (a count of holders or votes)


_MEASURES_COMMON = {
    "NumberOfShareholders": Measure("shareholders", "pure"),
    "NumberOfFullyPaidUpEquityShares": Measure("fully_paid_up_shares", "shares"),
    "NumberOfSharesUnderlyingOutstandingDepositoryReceipts": Measure("depository_receipt_shares", "shares"),
    "NumberOfShares": Measure("total_shares", "shares"),
    "NumberOfVotingRights": Measure("voting_rights", "pure"),
    "NumberOfVotingRightsHeldBySameClassOfSecurities": Measure("voting_rights_same_class", "pure"),
    "NumberOfTheLockedInShares": Measure("locked_in_shares", "shares"),
    "NumberOfEquitySharesHeldInDematerializedForm": Measure("demat_shares", "shares"),
}

MEASURES: dict[Layout, dict[str, Measure]] = {
    Layout.LAYOUT_2020: _MEASURES_COMMON
    | {
        "NumberOfPartlyPaidUpEquityShares": Measure("partly_paid_up_shares", "shares"),
        "NumberOfVotingRightsHeldByDifferentialVotingRights": Measure("voting_rights_differential", "pure"),
        "NumberOfSharesUnderlyingOutstandingConvertibleSecurities": Measure("convertible_securities_shares", "shares"),
        "NumberOfWarrants": Measure("warrant_shares", "shares"),
        "NumberOfConvertibleSecuritiesAndWarrants": Measure("convertible_and_warrant_shares", "shares"),
        # "pledged or otherwise encumbered": the same total as 2025's NumberOfSharesEncumbered
        "PledgedOrEncumberedNumberOfShares": Measure("encumbered_shares", "shares"),
    },
    Layout.LAYOUT_2025: _MEASURES_COMMON
    | {
        "NumberOfSharesOutstandingESOPGranted": Measure("esop_outstanding_shares", "shares"),
        "NumberOfSharesUnderlyingOutstandingConvertibleSecuritiesWarrantsAndESOP": Measure(
            "convertible_warrant_esop_shares", "shares"
        ),
        "NumberOfSharesOnFullyDilutedBasisIncludingWarrantsESOPAndConvertibleSecurities": Measure(
            "fully_diluted_shares", "shares"
        ),
        "NumberOfSharesEncumbered": Measure("encumbered_shares", "shares"),
        "NumberOfSharesEncumberedUnderPledged": Measure("pledged_shares", "shares"),
        "NumberOfSharesEncumberedUnderNonDisposalUndertaking": Measure("ndu_encumbered_shares", "shares"),
        "NumberOfSharesEncumberedUnderOtherEncumbrances": Measure("other_encumbered_shares", "shares"),
        # Shares by sub-category of shareholder (SEBI's 2023 sub-categorisation columns)
        "NumberOfSharesUnderSubCategoryOne": Measure("sub_category_1_shares", "shares"),
        "NumberOfSharesUnderSubCategoryTwo": Measure("sub_category_2_shares", "shares"),
        "NumberOfSharesUnderSubCategoryThree": Measure("sub_category_3_shares", "shares"),
    },
}

# Percentages, recomputed by code from the counts, so not stored. Counted in
# shareholding_filing.percentage_facts_skipped; any other element is quarantined.
PERCENTAGES: dict[Layout, frozenset[str]] = {
    Layout.LAYOUT_2020: frozenset(
        {
            "ShareholdingAsAPercentageOfTotalNumberOfShares",
            "PercentageOfTotalVotingRights",
            "ShareholdingAsAPercentageAssumingFullConversionOfConvertibleSecuritiesAndWarrants",
            "LockedInSharesAsAPercentageOfTotalNumberOfShares",
            "PledgedOrEncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
        }
    ),
    Layout.LAYOUT_2025: frozenset(
        {
            "ShareholdingAsAPercentageOfTotalNumberOfShares",
            "PercentageOfTotalVotingRights",
            "ShareholdingAsAPercentageAssumingFullConversionOfConvertibleSecuritiesWarrantsAndESOP",
            "LockedInSharesAsAPercentageOfTotalNumberOfShares",
            "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares",
            "EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares",
            "EncumberedShareUnderNonDisposalUndertakingAsPercentageOfTotalNumberOfShares",
            "EncumberedShareUnderOtherEncumbrancesAsPercentageOfTotalNumberOfShares",
            # Foreign-investment limits, reported in plain contexts for this and past quarters
            "PercentageOfBoardApprovedLimits",
            "PercentageOfLimitsUtilized",
        }
    ),
}

UNITS = {
    (("http://www.xbrl.org/2003/instance", "shares"),): "shares",
    (("http://www.xbrl.org/2003/instance", "pure"),): "pure",
}
