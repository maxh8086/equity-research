"""Tests for core.compute.ratings: scale tables, map_rating, direction, severity."""

import pytest

from core.compute.ratings import (
    SCALE_ORDER,
    compute_rating_direction,
    compute_severity,
    map_rating,
)
from core.db.models import CommonRatingScale, RatingAgency, RatingOutlook


# ---------------------------------------------------------------------------
# map_rating: CRISIL
# ---------------------------------------------------------------------------


class TestMapRatingCrisil:
    def test_aaa(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AAA")
        assert scale is CommonRatingScale.AAA
        assert outlook is None

    def test_aa_plus(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AA+")
        assert scale is CommonRatingScale.AA_PLUS

    def test_aa_minus(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AA-")
        assert scale is CommonRatingScale.AA_MINUS

    def test_bbb_minus(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL BBB-")
        assert scale is CommonRatingScale.BBB_MINUS

    def test_d(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL D")
        assert scale is CommonRatingScale.D

    def test_withdrawn(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL WITHDRAWN")
        assert scale is CommonRatingScale.WITHDRAWN

    def test_with_stable_outlook_slash(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AAA/Stable")
        assert scale is CommonRatingScale.AAA
        assert outlook is RatingOutlook.STABLE

    def test_with_negative_outlook_paren(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AA+ (Negative)")
        assert scale is CommonRatingScale.AA_PLUS
        assert outlook is RatingOutlook.NEGATIVE

    def test_with_watch_positive(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "CRISIL AA/Watch Positive")
        assert scale is CommonRatingScale.AA
        assert outlook is RatingOutlook.WATCH_POSITIVE

    def test_lowercase_normalised(self) -> None:
        scale, outlook = map_rating(RatingAgency.CRISIL, "crisil aaa")
        assert scale is CommonRatingScale.AAA

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown rating string"):
            map_rating(RatingAgency.CRISIL, "CRISIL ZZZ")


# ---------------------------------------------------------------------------
# map_rating: ICRA
# ---------------------------------------------------------------------------


class TestMapRatingIcra:
    def test_bracket_format(self) -> None:
        scale, _ = map_rating(RatingAgency.ICRA, "[ICRA]AAA")
        assert scale is CommonRatingScale.AAA

    def test_plain_format(self) -> None:
        scale, _ = map_rating(RatingAgency.ICRA, "ICRA AA+")
        assert scale is CommonRatingScale.AA_PLUS

    def test_bracket_d(self) -> None:
        scale, _ = map_rating(RatingAgency.ICRA, "[ICRA]D")
        assert scale is CommonRatingScale.D

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            map_rating(RatingAgency.ICRA, "ICRA XYZ")


# ---------------------------------------------------------------------------
# map_rating: CARE
# ---------------------------------------------------------------------------


class TestMapRatingCare:
    def test_aaa(self) -> None:
        scale, _ = map_rating(RatingAgency.CARE, "CARE AAA")
        assert scale is CommonRatingScale.AAA

    def test_bbb_plus(self) -> None:
        scale, _ = map_rating(RatingAgency.CARE, "CARE BBB+")
        assert scale is CommonRatingScale.BBB_PLUS

    def test_stable_outlook(self) -> None:
        scale, outlook = map_rating(RatingAgency.CARE, "CARE AAA/Stable")
        assert scale is CommonRatingScale.AAA
        assert outlook is RatingOutlook.STABLE


# ---------------------------------------------------------------------------
# map_rating: India Ratings
# ---------------------------------------------------------------------------


class TestMapRatingIndiaRatings:
    def test_aaa(self) -> None:
        scale, _ = map_rating(RatingAgency.INDIA_RATINGS, "IND AAA")
        assert scale is CommonRatingScale.AAA

    def test_bb_minus(self) -> None:
        scale, _ = map_rating(RatingAgency.INDIA_RATINGS, "IND BB-")
        assert scale is CommonRatingScale.BB_MINUS

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            map_rating(RatingAgency.INDIA_RATINGS, "IND ???")


# ---------------------------------------------------------------------------
# compute_severity
# ---------------------------------------------------------------------------


class TestComputeSeverity:
    def test_withdrawn_returns_2(self) -> None:
        assert compute_severity(CommonRatingScale.WITHDRAWN, True, False, False) == 2

    def test_withdrawn_flag_overrides(self) -> None:
        # Even if scale is AAA but is_withdrawn, severity is 2
        assert compute_severity(CommonRatingScale.AAA, True, False, False) == 2

    def test_downgrade_to_d_returns_5(self) -> None:
        assert compute_severity(CommonRatingScale.D, False, True, False) == 5

    def test_downgrade_to_b_returns_4(self) -> None:
        assert compute_severity(CommonRatingScale.B, False, True, False) == 4

    def test_downgrade_to_b_minus_returns_4(self) -> None:
        assert compute_severity(CommonRatingScale.B_MINUS, False, True, False) == 4

    def test_downgrade_to_c_returns_4(self) -> None:
        assert compute_severity(CommonRatingScale.C, False, True, False) == 4

    def test_downgrade_to_bb_returns_3(self) -> None:
        assert compute_severity(CommonRatingScale.BB, False, True, False) == 3

    def test_downgrade_to_bb_plus_returns_3(self) -> None:
        assert compute_severity(CommonRatingScale.BB_PLUS, False, True, False) == 3

    def test_downgrade_to_bb_minus_returns_3(self) -> None:
        assert compute_severity(CommonRatingScale.BB_MINUS, False, True, False) == 3

    def test_upgrade_returns_1(self) -> None:
        assert compute_severity(CommonRatingScale.A, False, False, True) == 1

    def test_first_aaa_returns_1(self) -> None:
        # is_upgrade=False, is_downgrade=False but scale is AAA -> 1
        assert compute_severity(CommonRatingScale.AAA, False, False, False) == 1

    def test_first_aa_returns_1(self) -> None:
        assert compute_severity(CommonRatingScale.AA, False, False, False) == 1

    def test_first_aa_plus_returns_1(self) -> None:
        assert compute_severity(CommonRatingScale.AA_PLUS, False, False, False) == 1

    def test_first_aa_minus_returns_1(self) -> None:
        assert compute_severity(CommonRatingScale.AA_MINUS, False, False, False) == 1

    def test_stable_action_returns_2(self) -> None:
        # No upgrade, no downgrade, no withdrawal, scale is A
        assert compute_severity(CommonRatingScale.A, False, False, False) == 2

    def test_downgrade_to_a_returns_2(self) -> None:
        # Downgrade, but to A (investment grade, above BB): falls through to 2
        assert compute_severity(CommonRatingScale.A, False, True, False) == 2


# ---------------------------------------------------------------------------
# compute_rating_direction
# ---------------------------------------------------------------------------


class TestComputeRatingDirection:
    def test_first_rating_returns_none(self) -> None:
        is_up, is_down = compute_rating_direction(None, CommonRatingScale.AAA)
        assert is_up is None
        assert is_down is None

    def test_downgrade(self) -> None:
        is_up, is_down = compute_rating_direction(CommonRatingScale.AAA, CommonRatingScale.AA)
        assert is_up is False
        assert is_down is True

    def test_upgrade(self) -> None:
        is_up, is_down = compute_rating_direction(CommonRatingScale.BB, CommonRatingScale.BBB_MINUS)
        assert is_up is True
        assert is_down is False

    def test_no_change(self) -> None:
        is_up, is_down = compute_rating_direction(CommonRatingScale.A, CommonRatingScale.A)
        assert is_up is False
        assert is_down is False

    def test_not_rated_prior_returns_none(self) -> None:
        # NOT_RATED is not in SCALE_ORDER
        is_up, is_down = compute_rating_direction(
            CommonRatingScale.NOT_RATED, CommonRatingScale.AAA
        )
        assert is_up is None
        assert is_down is None

    def test_withdrawn_current_returns_none(self) -> None:
        # WITHDRAWN is in SCALE_ORDER (0), so this should actually compute
        is_up, is_down = compute_rating_direction(CommonRatingScale.A, CommonRatingScale.WITHDRAWN)
        assert is_down is True

    def test_suspended_returns_none(self) -> None:
        # SUSPENDED is not in SCALE_ORDER
        is_up, is_down = compute_rating_direction(
            CommonRatingScale.A, CommonRatingScale.SUSPENDED
        )
        assert is_up is None
        assert is_down is None


# ---------------------------------------------------------------------------
# SCALE_ORDER is a strict total ordering (no ties between different scales)
# ---------------------------------------------------------------------------


class TestScaleOrder:
    def test_no_ties_between_different_scales(self) -> None:
        """Every pair of distinct scales in SCALE_ORDER must have different ordinal values."""
        seen: dict[int, CommonRatingScale] = {}
        for scale, value in SCALE_ORDER.items():
            if value in seen:
                pytest.fail(
                    f"SCALE_ORDER has a tie: {scale!r} and {seen[value]!r} both have value {value}"
                )
            seen[value] = scale

    def test_aaa_is_highest(self) -> None:
        aaa_val = SCALE_ORDER[CommonRatingScale.AAA]
        for scale, val in SCALE_ORDER.items():
            if scale is not CommonRatingScale.AAA:
                assert val < aaa_val, f"{scale} should be lower than AAA"

    def test_withdrawn_is_zero(self) -> None:
        assert SCALE_ORDER[CommonRatingScale.WITHDRAWN] == 0

    def test_d_is_one(self) -> None:
        assert SCALE_ORDER[CommonRatingScale.D] == 1

    def test_aaa_is_twenty(self) -> None:
        assert SCALE_ORDER[CommonRatingScale.AAA] == 20

    def test_investment_grade_above_speculative(self) -> None:
        assert SCALE_ORDER[CommonRatingScale.BBB_MINUS] > SCALE_ORDER[CommonRatingScale.BB_PLUS]

    def test_bb_range_above_b_range(self) -> None:
        assert SCALE_ORDER[CommonRatingScale.BB_MINUS] > SCALE_ORDER[CommonRatingScale.B_PLUS]
