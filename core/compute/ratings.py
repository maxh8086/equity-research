"""Credit rating scale tables and direction/severity computations (CLAUDE.md R1).

Rating scale tables are hardcoded by design, not config (CLAUDE.md "Hardcoded
by design"). Severity is computed by code; no model ever assigns it.

Each agency uses its own raw string format. `map_rating` normalises a raw
agency string to a `CommonRatingScale` entry and an optional `RatingOutlook`.
"""

from __future__ import annotations

import re
from enum import StrEnum


class RatingAgency(StrEnum):
    CRISIL = "crisil"
    ICRA = "icra"
    CARE = "care"
    INDIA_RATINGS = "india_ratings"
    BRICKWORK = "brickwork"
    ACUITE = "acuite"


class CommonRatingScale(StrEnum):
    AAA = "aaa"
    AA_PLUS = "aa_plus"
    AA = "aa"
    AA_MINUS = "aa_minus"
    A_PLUS = "a_plus"
    A = "a"
    A_MINUS = "a_minus"
    BBB_PLUS = "bbb_plus"
    BBB = "bbb"
    BBB_MINUS = "bbb_minus"
    BB_PLUS = "bb_plus"
    BB = "bb"
    BB_MINUS = "bb_minus"
    B_PLUS = "b_plus"
    B = "b"
    B_MINUS = "b_minus"
    C = "c"
    D = "d"
    WITHDRAWN = "withdrawn"
    SUSPENDED = "suspended"
    NOT_RATED = "not_rated"


class RatingOutlook(StrEnum):
    STABLE = "stable"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    WATCH_POSITIVE = "watch_positive"
    WATCH_NEGATIVE = "watch_negative"
    DEVELOPING = "developing"

# ---------------------------------------------------------------------------
# Agency-specific raw-string → CommonRatingScale tables
# All keys are upper-cased; map_rating normalises before lookup.
# ---------------------------------------------------------------------------

CRISIL_SCALE: dict[str, CommonRatingScale] = {
    "CRISIL AAA": CommonRatingScale.AAA,
    "CRISIL AA+": CommonRatingScale.AA_PLUS,
    "CRISIL AA": CommonRatingScale.AA,
    "CRISIL AA-": CommonRatingScale.AA_MINUS,
    "CRISIL A+": CommonRatingScale.A_PLUS,
    "CRISIL A": CommonRatingScale.A,
    "CRISIL A-": CommonRatingScale.A_MINUS,
    "CRISIL BBB+": CommonRatingScale.BBB_PLUS,
    "CRISIL BBB": CommonRatingScale.BBB,
    "CRISIL BBB-": CommonRatingScale.BBB_MINUS,
    "CRISIL BB+": CommonRatingScale.BB_PLUS,
    "CRISIL BB": CommonRatingScale.BB,
    "CRISIL BB-": CommonRatingScale.BB_MINUS,
    "CRISIL B+": CommonRatingScale.B_PLUS,
    "CRISIL B": CommonRatingScale.B,
    "CRISIL B-": CommonRatingScale.B_MINUS,
    "CRISIL C": CommonRatingScale.C,
    "CRISIL D": CommonRatingScale.D,
    "CRISIL WITHDRAWN": CommonRatingScale.WITHDRAWN,
    "CRISIL SUSPENDED": CommonRatingScale.SUSPENDED,
    "CRISIL NR": CommonRatingScale.NOT_RATED,
}

ICRA_SCALE: dict[str, CommonRatingScale] = {
    "ICRA AAA": CommonRatingScale.AAA,
    "[ICRA]AAA": CommonRatingScale.AAA,
    "ICRA AA+": CommonRatingScale.AA_PLUS,
    "[ICRA]AA+": CommonRatingScale.AA_PLUS,
    "ICRA AA": CommonRatingScale.AA,
    "[ICRA]AA": CommonRatingScale.AA,
    "ICRA AA-": CommonRatingScale.AA_MINUS,
    "[ICRA]AA-": CommonRatingScale.AA_MINUS,
    "ICRA A+": CommonRatingScale.A_PLUS,
    "[ICRA]A+": CommonRatingScale.A_PLUS,
    "ICRA A": CommonRatingScale.A,
    "[ICRA]A": CommonRatingScale.A,
    "ICRA A-": CommonRatingScale.A_MINUS,
    "[ICRA]A-": CommonRatingScale.A_MINUS,
    "ICRA BBB+": CommonRatingScale.BBB_PLUS,
    "[ICRA]BBB+": CommonRatingScale.BBB_PLUS,
    "ICRA BBB": CommonRatingScale.BBB,
    "[ICRA]BBB": CommonRatingScale.BBB,
    "ICRA BBB-": CommonRatingScale.BBB_MINUS,
    "[ICRA]BBB-": CommonRatingScale.BBB_MINUS,
    "ICRA BB+": CommonRatingScale.BB_PLUS,
    "[ICRA]BB+": CommonRatingScale.BB_PLUS,
    "ICRA BB": CommonRatingScale.BB,
    "[ICRA]BB": CommonRatingScale.BB,
    "ICRA BB-": CommonRatingScale.BB_MINUS,
    "[ICRA]BB-": CommonRatingScale.BB_MINUS,
    "ICRA B+": CommonRatingScale.B_PLUS,
    "[ICRA]B+": CommonRatingScale.B_PLUS,
    "ICRA B": CommonRatingScale.B,
    "[ICRA]B": CommonRatingScale.B,
    "ICRA B-": CommonRatingScale.B_MINUS,
    "[ICRA]B-": CommonRatingScale.B_MINUS,
    "ICRA C": CommonRatingScale.C,
    "[ICRA]C": CommonRatingScale.C,
    "ICRA D": CommonRatingScale.D,
    "[ICRA]D": CommonRatingScale.D,
    "ICRA WITHDRAWN": CommonRatingScale.WITHDRAWN,
    "[ICRA]WITHDRAWN": CommonRatingScale.WITHDRAWN,
}

CARE_SCALE: dict[str, CommonRatingScale] = {
    "CARE AAA": CommonRatingScale.AAA,
    "CARE AA+": CommonRatingScale.AA_PLUS,
    "CARE AA": CommonRatingScale.AA,
    "CARE AA-": CommonRatingScale.AA_MINUS,
    "CARE A+": CommonRatingScale.A_PLUS,
    "CARE A": CommonRatingScale.A,
    "CARE A-": CommonRatingScale.A_MINUS,
    "CARE BBB+": CommonRatingScale.BBB_PLUS,
    "CARE BBB": CommonRatingScale.BBB,
    "CARE BBB-": CommonRatingScale.BBB_MINUS,
    "CARE BB+": CommonRatingScale.BB_PLUS,
    "CARE BB": CommonRatingScale.BB,
    "CARE BB-": CommonRatingScale.BB_MINUS,
    "CARE B+": CommonRatingScale.B_PLUS,
    "CARE B": CommonRatingScale.B,
    "CARE B-": CommonRatingScale.B_MINUS,
    "CARE C": CommonRatingScale.C,
    "CARE D": CommonRatingScale.D,
    "CARE WITHDRAWN": CommonRatingScale.WITHDRAWN,
}

IND_SCALE: dict[str, CommonRatingScale] = {
    "IND AAA": CommonRatingScale.AAA,
    "IND AA+": CommonRatingScale.AA_PLUS,
    "IND AA": CommonRatingScale.AA,
    "IND AA-": CommonRatingScale.AA_MINUS,
    "IND A+": CommonRatingScale.A_PLUS,
    "IND A": CommonRatingScale.A,
    "IND A-": CommonRatingScale.A_MINUS,
    "IND BBB+": CommonRatingScale.BBB_PLUS,
    "IND BBB": CommonRatingScale.BBB,
    "IND BBB-": CommonRatingScale.BBB_MINUS,
    "IND BB+": CommonRatingScale.BB_PLUS,
    "IND BB": CommonRatingScale.BB,
    "IND BB-": CommonRatingScale.BB_MINUS,
    "IND B+": CommonRatingScale.B_PLUS,
    "IND B": CommonRatingScale.B,
    "IND B-": CommonRatingScale.B_MINUS,
    "IND C": CommonRatingScale.C,
    "IND D": CommonRatingScale.D,
    "IND WITHDRAWN": CommonRatingScale.WITHDRAWN,
}

BRICKWORK_SCALE: dict[str, CommonRatingScale] = {
    "BWR AAA": CommonRatingScale.AAA,
    "BWR AA+": CommonRatingScale.AA_PLUS,
    "BWR AA": CommonRatingScale.AA,
    "BWR AA-": CommonRatingScale.AA_MINUS,
    "BWR A+": CommonRatingScale.A_PLUS,
    "BWR A": CommonRatingScale.A,
    "BWR A-": CommonRatingScale.A_MINUS,
    "BWR BBB+": CommonRatingScale.BBB_PLUS,
    "BWR BBB": CommonRatingScale.BBB,
    "BWR BBB-": CommonRatingScale.BBB_MINUS,
    "BWR BB+": CommonRatingScale.BB_PLUS,
    "BWR BB": CommonRatingScale.BB,
    "BWR BB-": CommonRatingScale.BB_MINUS,
    "BWR B+": CommonRatingScale.B_PLUS,
    "BWR B": CommonRatingScale.B,
    "BWR B-": CommonRatingScale.B_MINUS,
    "BWR C": CommonRatingScale.C,
    "BWR D": CommonRatingScale.D,
    "BWR WITHDRAWN": CommonRatingScale.WITHDRAWN,
}

ACUITE_SCALE: dict[str, CommonRatingScale] = {
    "ACUITE AAA": CommonRatingScale.AAA,
    "ACUITE AA+": CommonRatingScale.AA_PLUS,
    "ACUITE AA": CommonRatingScale.AA,
    "ACUITE AA-": CommonRatingScale.AA_MINUS,
    "ACUITE A+": CommonRatingScale.A_PLUS,
    "ACUITE A": CommonRatingScale.A,
    "ACUITE A-": CommonRatingScale.A_MINUS,
    "ACUITE BBB+": CommonRatingScale.BBB_PLUS,
    "ACUITE BBB": CommonRatingScale.BBB,
    "ACUITE BBB-": CommonRatingScale.BBB_MINUS,
    "ACUITE BB+": CommonRatingScale.BB_PLUS,
    "ACUITE BB": CommonRatingScale.BB,
    "ACUITE BB-": CommonRatingScale.BB_MINUS,
    "ACUITE B+": CommonRatingScale.B_PLUS,
    "ACUITE B": CommonRatingScale.B,
    "ACUITE B-": CommonRatingScale.B_MINUS,
    "ACUITE C": CommonRatingScale.C,
    "ACUITE D": CommonRatingScale.D,
    "ACUITE WITHDRAWN": CommonRatingScale.WITHDRAWN,
}

_AGENCY_SCALES: dict[RatingAgency, dict[str, CommonRatingScale]] = {
    RatingAgency.CRISIL: CRISIL_SCALE,
    RatingAgency.ICRA: ICRA_SCALE,
    RatingAgency.CARE: CARE_SCALE,
    RatingAgency.INDIA_RATINGS: IND_SCALE,
    RatingAgency.BRICKWORK: BRICKWORK_SCALE,
    RatingAgency.ACUITE: ACUITE_SCALE,
}

# ---------------------------------------------------------------------------
# Outlook extraction patterns
# "/Stable", "/ Stable", "(Stable)", " Stable" at end of string
# ---------------------------------------------------------------------------

_OUTLOOK_MAP: dict[str, RatingOutlook] = {
    "STABLE": RatingOutlook.STABLE,
    "POSITIVE": RatingOutlook.POSITIVE,
    "NEGATIVE": RatingOutlook.NEGATIVE,
    "WATCH POSITIVE": RatingOutlook.WATCH_POSITIVE,
    "WATCH_POSITIVE": RatingOutlook.WATCH_POSITIVE,
    "POSITIVE WATCH": RatingOutlook.WATCH_POSITIVE,
    "WATCH NEGATIVE": RatingOutlook.WATCH_NEGATIVE,
    "WATCH_NEGATIVE": RatingOutlook.WATCH_NEGATIVE,
    "NEGATIVE WATCH": RatingOutlook.WATCH_NEGATIVE,
    "DEVELOPING": RatingOutlook.DEVELOPING,
    "CREDITWATCH POSITIVE": RatingOutlook.WATCH_POSITIVE,
    "CREDITWATCH NEGATIVE": RatingOutlook.WATCH_NEGATIVE,
    "RATING WATCH POSITIVE": RatingOutlook.WATCH_POSITIVE,
    "RATING WATCH NEGATIVE": RatingOutlook.WATCH_NEGATIVE,
    "UNDER WATCH": RatingOutlook.DEVELOPING,
}

# Matches: "/Stable", "/ Stable", "(Stable)", "(Watch Positive)" etc.
_OUTLOOK_SUFFIX_RE = re.compile(
    r"""
    \s*                          # optional leading whitespace
    (?:/\s*|\(|\s+)              # separator: slash, open-paren, or whitespace
    ([A-Za-z][A-Za-z _]*)        # the outlook text
    \)?                          # optional closing paren
    \s*$                         # end of string
    """,
    re.VERBOSE,
)


def map_rating(
    agency: RatingAgency, raw_rating: str
) -> tuple[CommonRatingScale, RatingOutlook | None]:
    """Map an agency-specific raw rating string to the common scale and an optional outlook.

    Normalises by stripping and upper-casing. Extracts the outlook from a
    slash-suffix ("CRISIL AAA/Stable") or parenthetical ("CRISIL AA (Positive)").
    Raises `ValueError` if the rating part is not found in the agency's hardcoded table.
    """
    normalised = raw_rating.strip().upper()

    # Try to peel off an outlook suffix.
    outlook: RatingOutlook | None = None
    rating_part = normalised

    m = _OUTLOOK_SUFFIX_RE.search(normalised)
    if m:
        candidate = m.group(1).strip().upper()
        if candidate in _OUTLOOK_MAP:
            outlook = _OUTLOOK_MAP[candidate]
            rating_part = normalised[: m.start()].strip()

    scale_table = _AGENCY_SCALES[agency]
    if rating_part not in scale_table:
        raise ValueError(
            f"Unknown rating string for agency {agency!r}: {raw_rating!r} "
            f"(normalised rating part: {rating_part!r})"
        )

    return scale_table[rating_part], outlook


# ---------------------------------------------------------------------------
# Scale ordering (hardcoded by design — CLAUDE.md)
# SUSPENDED and NOT_RATED are not placed in the ordering; functions treat
# them as incomparable and return None, None for direction.
# ---------------------------------------------------------------------------

SCALE_ORDER: dict[CommonRatingScale, int] = {
    CommonRatingScale.AAA: 20,
    CommonRatingScale.AA_PLUS: 19,
    CommonRatingScale.AA: 18,
    CommonRatingScale.AA_MINUS: 17,
    CommonRatingScale.A_PLUS: 16,
    CommonRatingScale.A: 15,
    CommonRatingScale.A_MINUS: 14,
    CommonRatingScale.BBB_PLUS: 13,
    CommonRatingScale.BBB: 12,
    CommonRatingScale.BBB_MINUS: 11,
    CommonRatingScale.BB_PLUS: 10,
    CommonRatingScale.BB: 9,
    CommonRatingScale.BB_MINUS: 8,
    CommonRatingScale.B_PLUS: 7,
    CommonRatingScale.B: 6,
    CommonRatingScale.B_MINUS: 5,
    CommonRatingScale.C: 3,
    CommonRatingScale.D: 1,
    CommonRatingScale.WITHDRAWN: 0,
}


def compute_rating_direction(
    prior: CommonRatingScale | None,
    current: CommonRatingScale,
) -> tuple[bool | None, bool | None]:
    """Compute (is_upgrade, is_downgrade) by comparing scale positions.

    Returns (None, None) if `prior` is None (first rating) or if either scale
    is not in SCALE_ORDER (i.e. SUSPENDED or NOT_RATED, which are not ordered).
    Never returns (True, True).
    """
    if prior is None:
        return None, None
    prior_pos = SCALE_ORDER.get(prior)
    current_pos = SCALE_ORDER.get(current)
    if prior_pos is None or current_pos is None:
        return None, None
    if current_pos > prior_pos:
        return True, False
    if current_pos < prior_pos:
        return False, True
    return False, False


# Thresholds used by compute_severity (hardcoded — CLAUDE.md)
_B_ORDER = SCALE_ORDER[CommonRatingScale.B]
_BB_MINUS_ORDER = SCALE_ORDER[CommonRatingScale.BB_MINUS]
_BB_PLUS_ORDER = SCALE_ORDER[CommonRatingScale.BB_PLUS]


def compute_severity(
    common_scale: CommonRatingScale,
    is_withdrawn: bool,
    is_downgrade: bool,
    is_upgrade: bool,
) -> int:
    """Compute a severity score 1–5 from the rating and direction.

    Rules (CLAUDE.md):
    - withdrawn → 2
    - downgrade to D → 5
    - downgrade to B or below (B, B_MINUS, C) → 4
    - downgrade to BB range (BB_PLUS, BB, BB_MINUS) → 3
    - upgrade or first rating to AAA or AA family → 1
    - everything else → 2
    """
    if is_withdrawn or common_scale is CommonRatingScale.WITHDRAWN:
        return 2

    if is_downgrade:
        if common_scale is CommonRatingScale.D:
            return 5
        current_pos = SCALE_ORDER.get(common_scale)
        if current_pos is not None:
            if current_pos <= _B_ORDER:
                return 4
            if _BB_MINUS_ORDER <= current_pos <= _BB_PLUS_ORDER:
                return 3

    if is_upgrade or common_scale in (
        CommonRatingScale.AAA,
        CommonRatingScale.AA_PLUS,
        CommonRatingScale.AA,
        CommonRatingScale.AA_MINUS,
    ):
        return 1

    return 2
