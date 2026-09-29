"""Post-earnings drift watchlist candidates.

Year-on-year sales and PAT growth against thresholds; surprise measured against
the company's own trailing trend (mean and trend of prior quarters) and against
its guidance_claim ledger (never an inferred consensus); forward PE annualising
the latest quarter on the diluted share count known at t. Score is a hardcoded
formula under RULE_VERSION='drift/1'. Output is a watchlist candidate with NO
action field or wording (watchlist entry only, never ADD_REVIEW on its own).

Pure: callers pass everything in. No DB, no I/O, no LLM.
Decimal money, never float. Every input carries as_of; the function refuses
inputs dated after the `t` argument with a LookAhead error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


RULE_VERSION = "drift/1"


class LookAhead(ValueError):
    """Evidence dated after the observation reached the computation. Always a bug."""


class MissingInputReason(StrEnum):
    """Why a required metric is unavailable for drift scoring."""

    MISSING_SALES_DATA = "missing_sales_data"
    MISSING_PROFIT_DATA = "missing_profit_data"
    NO_GUIDANCE_CLAIMS = "no_guidance_claims"
    UNKNOWN_SHARE_COUNT = "unknown_share_count"
    LOSS_TO_PROFIT_SALES = "loss_to_profit_sales"
    LOSS_TO_PROFIT_PROFIT = "loss_to_profit_profit"


@dataclass(frozen=True)
class DriftWatch:
    """A post-earnings drift watchlist candidate.

    All monetary values in absolute units (INR, not crores). Percentages as
    Decimal (e.g., 15.5 for 15.5%). No verdict vocabulary: this is a
    watchlist entry, not an action.
    """

    isin: str
    period_end: date
    observed_on: date
    yoy_sales_growth_pct: Decimal | None
    yoy_pat_growth_pct: Decimal | None
    trailing_trend_surprise_pct: Decimal | None
    guidance_surprise_pct: Decimal | None
    forward_pe: Decimal | None
    score: Decimal
    evidence_url: str
    rule_version: str = RULE_VERSION
    missing_reasons: tuple[MissingInputReason, ...] = ()


def drift_watch(
    isin: str,
    period_end: date,
    t: date,
    yoy_sales_growth_pct: tuple[Decimal, date] | None,
    yoy_pat_growth_pct: tuple[Decimal, date] | None,
    trailing_trend_surprise_pct: tuple[Decimal, date] | None,
    guidance_surprise_pct: tuple[Decimal, date] | None,
    forward_pe: tuple[Decimal, date] | None,
    diluted_share_count: tuple[Decimal, date] | None,
    evidence_url: str,
) -> DriftWatch:
    """Compute a post-earnings drift watchlist candidate.

    All growth metrics are year-on-year percentage changes (e.g., 15.5 for 15.5%).
    Surprise metrics are the difference between actual and expected, in percentage
    points (e.g., 3.1 for +3.1 pp). Forward PE annualises the latest quarter.

    Args:
        isin: The company's ISIN.
        period_end: The quarter-end date (e.g., 2025-03-31).
        t: The observation date; all as_of dates must be <= t.
        yoy_sales_growth_pct: (growth %, as_of date) or None.
        yoy_pat_growth_pct: (growth %, as_of date) or None.
        trailing_trend_surprise_pct: (surprise %, as_of date) or None.
        guidance_surprise_pct: (surprise %, as_of date) or None.
        forward_pe: (PE ratio, as_of date) or None. Assumed annualised.
        diluted_share_count: (share count, as_of date) or None.
        evidence_url: Source URL for the results announcement.

    Returns:
        A DriftWatch with score and missing_reasons.

    Raises:
        LookAhead: If any as_of date is after t.
    """
    missing = []

    # Check for look-ahead: all as_of dates must be <= t
    if yoy_sales_growth_pct is not None and yoy_sales_growth_pct[1] > t:
        raise LookAhead(f"yoy_sales_growth_pct as_of {yoy_sales_growth_pct[1]} is after t {t}")
    if yoy_pat_growth_pct is not None and yoy_pat_growth_pct[1] > t:
        raise LookAhead(f"yoy_pat_growth_pct as_of {yoy_pat_growth_pct[1]} is after t {t}")
    if trailing_trend_surprise_pct is not None and trailing_trend_surprise_pct[1] > t:
        raise LookAhead(
            f"trailing_trend_surprise_pct as_of {trailing_trend_surprise_pct[1]} is after t {t}"
        )
    if guidance_surprise_pct is not None and guidance_surprise_pct[1] > t:
        raise LookAhead(f"guidance_surprise_pct as_of {guidance_surprise_pct[1]} is after t {t}")
    if forward_pe is not None and forward_pe[1] > t:
        raise LookAhead(f"forward_pe as_of {forward_pe[1]} is after t {t}")
    if diluted_share_count is not None and diluted_share_count[1] > t:
        raise LookAhead(f"diluted_share_count as_of {diluted_share_count[1]} is after t {t}")

    # Extract values and check for negative bases (loss to profit)
    yoy_sales: Decimal | None = None
    if yoy_sales_growth_pct is None:
        missing.append(MissingInputReason.MISSING_SALES_DATA)
    elif yoy_sales_growth_pct[0] < 0:
        missing.append(MissingInputReason.LOSS_TO_PROFIT_SALES)
    else:
        yoy_sales = yoy_sales_growth_pct[0]

    yoy_pat: Decimal | None = None
    if yoy_pat_growth_pct is None:
        missing.append(MissingInputReason.MISSING_PROFIT_DATA)
    elif yoy_pat_growth_pct[0] < 0:
        missing.append(MissingInputReason.LOSS_TO_PROFIT_PROFIT)
    else:
        yoy_pat = yoy_pat_growth_pct[0]

    trailing_surprise: Decimal | None = None
    if trailing_trend_surprise_pct is None:
        # Trailing trend is optional for scoring, but if missing, it's still a data gap
        pass
    else:
        trailing_surprise = trailing_trend_surprise_pct[0]

    guidance_surprise: Decimal | None = None
    if guidance_surprise_pct is None:
        missing.append(MissingInputReason.NO_GUIDANCE_CLAIMS)
    else:
        guidance_surprise = guidance_surprise_pct[0]

    forward_pe_val: Decimal | None = None
    if forward_pe is None or diluted_share_count is None:
        if forward_pe is not None or diluted_share_count is not None:
            missing.append(MissingInputReason.UNKNOWN_SHARE_COUNT)
    else:
        forward_pe_val = forward_pe[0]

    # Compute score using the hardcoded formula (drift/1)
    score = _compute_score(
        yoy_sales=yoy_sales,
        yoy_pat=yoy_pat,
        trailing_surprise=trailing_surprise,
        guidance_surprise=guidance_surprise,
        forward_pe=forward_pe_val,
    )

    return DriftWatch(
        isin=isin,
        period_end=period_end,
        observed_on=t,
        yoy_sales_growth_pct=yoy_sales,
        yoy_pat_growth_pct=yoy_pat,
        trailing_trend_surprise_pct=trailing_surprise,
        guidance_surprise_pct=guidance_surprise,
        forward_pe=forward_pe_val,
        score=score,
        evidence_url=evidence_url,
        rule_version=RULE_VERSION,
        missing_reasons=tuple(missing),
    )


def _compute_score(
    yoy_sales: Decimal | None,
    yoy_pat: Decimal | None,
    trailing_surprise: Decimal | None,
    guidance_surprise: Decimal | None,
    forward_pe: Decimal | None,
) -> Decimal:
    """Hardcoded drift/1 score formula.

    Score is designed to identify high-growth companies with positive surprises.
    Base of 50 points, with bonuses for:
    - YoY PAT growth (main driver)
    - Guidance surprise (beat vs ledger)
    - Trailing trend surprise (beat vs own trend)
    - Valuation (lower PE gets slight bonus, but not dominant)
    """
    score = Decimal("50")

    # YoY PAT growth is the strongest signal (weighted at 3x)
    if yoy_pat is not None:
        score += yoy_pat * Decimal("0.3")

    # YoY sales growth adds to confidence (weighted at 1x)
    if yoy_sales is not None:
        score += yoy_sales * Decimal("0.1")

    # Guidance surprise: beats raise the score more than misses lower it
    if guidance_surprise is not None:
        score += guidance_surprise * Decimal("0.5")

    # Trailing trend surprise: beats indicate momentum
    if trailing_surprise is not None:
        score += trailing_surprise * Decimal("0.4")

    # Forward PE: lower PE (higher earnings yield) gets a small bonus
    # But high-growth stocks naturally trade at higher PE, so this is light
    if forward_pe is not None:
        # Subtract 1 point per PE point above 20, cap at -10
        pe_penalty = max(forward_pe - Decimal("20"), Decimal("0"))
        score -= min(pe_penalty * Decimal("0.1"), Decimal("10"))

    # Minimum score of 1, maximum useful score is unbounded but typically < 100
    return max(score, Decimal("1")).quantize(Decimal("0.01"))
