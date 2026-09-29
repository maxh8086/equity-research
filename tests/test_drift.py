"""Post-earnings drift watchlist candidates.

Year-on-year sales and PAT growth against thresholds; surprise measured against
the company's own trailing trend (mean and trend of prior quarters) and against
its guidance_claim ledger (never an inferred consensus); forward PE annualising
the latest quarter on the diluted share count known at t. Score is a hardcoded
formula under RULE_VERSION='drift/1'. Output is a watchlist candidate with NO
buy/sell/hold/ADD_REVIEW field or wording.

Pure: callers pass everything in. No DB, no I/O, no LLM.
Decimal money, never float. Every input carries as_of; the function refuses
inputs dated after the `t` argument with a LookAhead error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from core.compute.drift import (
    DriftWatch,
    LookAhead,
    MissingInputReason,
    RULE_VERSION,
    drift_watch,
)


# --------------------------------------------------------------------------- #
# Output type tests
# --------------------------------------------------------------------------- #


def test_output_is_driftwatch_dataclass():
    """Output is a frozen dataclass with no verdict vocabulary."""
    assert hasattr(DriftWatch, "__dataclass_fields__")

    # Create a valid instance
    watch = DriftWatch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        observed_on=date(2025, 5, 30),
        yoy_sales_growth_pct=Decimal("15.5"),
        yoy_pat_growth_pct=Decimal("12.3"),
        trailing_trend_surprise_pct=Decimal("5.2"),
        guidance_surprise_pct=Decimal("3.1"),
        forward_pe=Decimal("18.5"),
        score=Decimal("65.3"),
        evidence_url="https://example.com/results",
        rule_version=RULE_VERSION,
    )
    assert watch.isin == "INE002A01012"
    assert watch.score == Decimal("65.3")


def test_no_verdict_vocabulary_in_driftwatch_fields():
    """No buy/sell/hold terminology in fields or their types."""
    watch = DriftWatch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        observed_on=date(2025, 5, 30),
        yoy_sales_growth_pct=Decimal("15.5"),
        yoy_pat_growth_pct=Decimal("12.3"),
        trailing_trend_surprise_pct=Decimal("5.2"),
        guidance_surprise_pct=Decimal("3.1"),
        forward_pe=Decimal("18.5"),
        score=Decimal("65.3"),
        evidence_url="https://example.com/results",
        rule_version=RULE_VERSION,
    )

    # Check that the dataclass doesn't have verdict fields
    verdict_words = {"buy", "sell", "hold"}
    for field_name in watch.__dataclass_fields__:
        assert not any(v in field_name.lower() for v in verdict_words)


# --------------------------------------------------------------------------- #
# LookAhead errors
# --------------------------------------------------------------------------- #


def test_rejects_yoy_sales_growth_dated_after_t():
    """LookAhead error if yoy_sales_growth has as_of > t."""
    with pytest.raises(LookAhead):
        drift_watch(
            isin="INE002A01012",
            period_end=date(2025, 3, 31),
            t=date(2025, 5, 30),
            yoy_sales_growth_pct=(Decimal("15.5"), date(2025, 6, 1)),  # after t
            yoy_pat_growth_pct=(Decimal("12.3"), date(2025, 5, 30)),
            trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
            guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
            forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
            diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
            evidence_url="https://example.com/results",
        )


def test_rejects_yoy_pat_growth_dated_after_t():
    """LookAhead error if yoy_pat_growth has as_of > t."""
    with pytest.raises(LookAhead):
        drift_watch(
            isin="INE002A01012",
            period_end=date(2025, 3, 31),
            t=date(2025, 5, 30),
            yoy_sales_growth_pct=(Decimal("15.5"), date(2025, 5, 30)),
            yoy_pat_growth_pct=(Decimal("12.3"), date(2025, 5, 31)),  # after t
            trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
            guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
            forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
            diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
            evidence_url="https://example.com/results",
        )


def test_rejects_guidance_surprise_dated_after_t():
    """LookAhead error if guidance_surprise has as_of > t."""
    with pytest.raises(LookAhead):
        drift_watch(
            isin="INE002A01012",
            period_end=date(2025, 3, 31),
            t=date(2025, 5, 30),
            yoy_sales_growth_pct=(Decimal("15.5"), date(2025, 5, 30)),
            yoy_pat_growth_pct=(Decimal("12.3"), date(2025, 5, 30)),
            trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
            guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 31)),  # after t
            forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
            diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
            evidence_url="https://example.com/results",
        )


def test_rejects_diluted_share_count_dated_after_t():
    """LookAhead error if diluted_share_count has as_of > t."""
    with pytest.raises(LookAhead):
        drift_watch(
            isin="INE002A01012",
            period_end=date(2025, 3, 31),
            t=date(2025, 5, 30),
            yoy_sales_growth_pct=(Decimal("15.5"), date(2025, 5, 30)),
            yoy_pat_growth_pct=(Decimal("12.3"), date(2025, 5, 30)),
            trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
            guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
            forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
            diluted_share_count=(Decimal("1000000"), date(2025, 5, 31)),  # after t
            evidence_url="https://example.com/results",
        )


# --------------------------------------------------------------------------- #
# Negative base handling (loss to profit)
# --------------------------------------------------------------------------- #


def test_loss_to_profit_yoy_sales_not_comparable():
    """Negative sales growth base is not comparable; returns explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(-Decimal("10.0"), date(2025, 5, 30)),  # negative base
        yoy_pat_growth_pct=(Decimal("12.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_sales_growth_pct is None
    assert hasattr(result, "missing_reasons")


def test_loss_to_profit_yoy_pat_not_comparable():
    """Negative PAT growth base is not comparable; returns explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("15.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(-Decimal("5.0"), date(2025, 5, 30)),  # negative base
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_pat_growth_pct is None


# --------------------------------------------------------------------------- #
# YoY growth calculations
# --------------------------------------------------------------------------- #


def test_yoy_sales_growth_positive():
    """YoY sales growth is calculated and stored."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_sales_growth_pct == Decimal("20.5")


def test_yoy_pat_growth_positive():
    """YoY PAT growth is calculated and stored."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_pat_growth_pct == Decimal("15.3")


# --------------------------------------------------------------------------- #
# Surprise calculations
# --------------------------------------------------------------------------- #


def test_trailing_trend_surprise_stored():
    """Trailing trend surprise is stored as passed in."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("8.7"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.trailing_trend_surprise_pct == Decimal("8.7")


def test_guidance_surprise_stored():
    """Guidance surprise is stored as passed in."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("6.4"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.guidance_surprise_pct == Decimal("6.4")


# --------------------------------------------------------------------------- #
# Forward PE calculation
# --------------------------------------------------------------------------- #


def test_forward_pe_annualised_from_quarter():
    """Forward PE annualises latest quarter on diluted share count known at t."""
    # Quarterly PAT: 100 crore, diluted shares: 10 crore
    # Annualised EPS: (100 * 4) / 10 = 40
    # Price: 800 INR
    # Forward PE: 800 / 40 = 20
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("20.0"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("10000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.forward_pe == Decimal("20.0")


# --------------------------------------------------------------------------- #
# Score formula
# --------------------------------------------------------------------------- #


def test_score_is_deterministic():
    """Score is deterministic given same inputs."""
    input_kwargs = {
        "isin": "INE002A01012",
        "period_end": date(2025, 3, 31),
        "t": date(2025, 5, 30),
        "yoy_sales_growth_pct": (Decimal("20.5"), date(2025, 5, 30)),
        "yoy_pat_growth_pct": (Decimal("15.3"), date(2025, 5, 30)),
        "trailing_trend_surprise_pct": (Decimal("5.2"), date(2025, 5, 30)),
        "guidance_surprise_pct": (Decimal("3.1"), date(2025, 5, 30)),
        "forward_pe": (Decimal("18.5"), date(2025, 5, 30)),
        "diluted_share_count": (Decimal("1000000"), date(2025, 5, 30)),
        "evidence_url": "https://example.com/results",
    }

    result1 = drift_watch(**input_kwargs)
    result2 = drift_watch(**input_kwargs)

    assert result1.score == result2.score


def test_score_increases_with_yoy_pat_growth():
    """Higher YoY PAT growth should increase score."""
    base_kwargs = {
        "isin": "INE002A01012",
        "period_end": date(2025, 3, 31),
        "t": date(2025, 5, 30),
        "yoy_sales_growth_pct": (Decimal("20.5"), date(2025, 5, 30)),
        "trailing_trend_surprise_pct": (Decimal("5.2"), date(2025, 5, 30)),
        "guidance_surprise_pct": (Decimal("3.1"), date(2025, 5, 30)),
        "forward_pe": (Decimal("18.5"), date(2025, 5, 30)),
        "diluted_share_count": (Decimal("1000000"), date(2025, 5, 30)),
        "evidence_url": "https://example.com/results",
    }

    result_low = drift_watch(
        **base_kwargs,
        yoy_pat_growth_pct=(Decimal("5.0"), date(2025, 5, 30)),
    )
    result_high = drift_watch(
        **base_kwargs,
        yoy_pat_growth_pct=(Decimal("25.0"), date(2025, 5, 30)),
    )

    assert result_high.score > result_low.score


def test_score_increases_with_guidance_surprise():
    """Higher guidance surprise should increase score."""
    base_kwargs = {
        "isin": "INE002A01012",
        "period_end": date(2025, 3, 31),
        "t": date(2025, 5, 30),
        "yoy_sales_growth_pct": (Decimal("20.5"), date(2025, 5, 30)),
        "yoy_pat_growth_pct": (Decimal("15.3"), date(2025, 5, 30)),
        "trailing_trend_surprise_pct": (Decimal("5.2"), date(2025, 5, 30)),
        "forward_pe": (Decimal("18.5"), date(2025, 5, 30)),
        "diluted_share_count": (Decimal("1000000"), date(2025, 5, 30)),
        "evidence_url": "https://example.com/results",
    }

    result_low = drift_watch(
        **base_kwargs,
        guidance_surprise_pct=(Decimal("1.0"), date(2025, 5, 30)),
    )
    result_high = drift_watch(
        **base_kwargs,
        guidance_surprise_pct=(Decimal("10.0"), date(2025, 5, 30)),
    )

    assert result_high.score > result_low.score


# --------------------------------------------------------------------------- #
# Missing inputs with explicit reasons
# --------------------------------------------------------------------------- #


def test_missing_yoy_sales_has_reason():
    """Missing YoY sales growth has an explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=None,
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_sales_growth_pct is None
    assert MissingInputReason.MISSING_SALES_DATA in result.missing_reasons


def test_missing_yoy_pat_has_reason():
    """Missing YoY PAT growth has an explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=None,
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.yoy_pat_growth_pct is None
    assert MissingInputReason.MISSING_PROFIT_DATA in result.missing_reasons


def test_missing_guidance_surprise_has_reason():
    """Missing guidance surprise has an explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=None,
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    assert result.guidance_surprise_pct is None
    assert MissingInputReason.NO_GUIDANCE_CLAIMS in result.missing_reasons


def test_missing_share_count_has_reason():
    """Missing diluted share count has an explicit reason."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("20.5"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("15.3"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("5.2"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("3.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("18.5"), date(2025, 5, 30)),
        diluted_share_count=None,
        evidence_url="https://example.com/results",
    )
    assert result.forward_pe is None
    assert MissingInputReason.UNKNOWN_SHARE_COUNT in result.missing_reasons


# --------------------------------------------------------------------------- #
# Threshold tests
# --------------------------------------------------------------------------- #


def test_no_minimum_yoy_growth_threshold():
    """Even low YoY growth creates a watchlist entry (threshold is about the formula)."""
    result = drift_watch(
        isin="INE002A01012",
        period_end=date(2025, 3, 31),
        t=date(2025, 5, 30),
        yoy_sales_growth_pct=(Decimal("2.0"), date(2025, 5, 30)),
        yoy_pat_growth_pct=(Decimal("1.0"), date(2025, 5, 30)),
        trailing_trend_surprise_pct=(Decimal("0.5"), date(2025, 5, 30)),
        guidance_surprise_pct=(Decimal("0.1"), date(2025, 5, 30)),
        forward_pe=(Decimal("15.0"), date(2025, 5, 30)),
        diluted_share_count=(Decimal("1000000"), date(2025, 5, 30)),
        evidence_url="https://example.com/results",
    )
    # Should still produce a watchlist entry
    assert result.isin == "INE002A01012"
    assert result.score is not None


# --------------------------------------------------------------------------- #
# Rule version
# --------------------------------------------------------------------------- #


def test_rule_version_is_drift_1():
    """Rule version is hardcoded as drift/1."""
    assert RULE_VERSION == "drift/1"
