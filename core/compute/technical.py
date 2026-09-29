"""Technical screen detectors: volume spikes, consolidation breakouts, ATH breakouts.

Pure functions, no I/O. All prices must already be adjusted for corporate
actions before being passed in. Volumes are raw (integer). All price
arithmetic uses Decimal throughout (R1: never float).

Rule version: technical_v1
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

RULE_VERSION = "technical_v1"


@dataclass(frozen=True)
class DailyBar:
    """One day's adjusted OHLCV bar.

    `close` is already adjusted for all corporate actions whose ex_date
    is on or before this bar's date (using only adjustment factors known at
    the time of the query -- R2 is enforced by the caller, not here).
    `adjustment_factor` is the cumulative factor applied.
    """

    date: date
    close: Decimal
    volume: int
    adjustment_factor: Decimal


@dataclass(frozen=True)
class TechnicalSignalResult:
    """One detected technical signal. All fields mirror the technical_signal table."""

    isin: str
    signal_type: str  # TechnicalSignalType value
    signal_date: date
    close_price: Decimal
    volume: int
    volume_median_50d: Decimal | None
    price_return_1d: Decimal | None
    consolidation_start: date | None
    consolidation_high: Decimal | None
    consolidation_low: Decimal | None
    ath_since: date | None
    adjustment_factor: Decimal
    rule_version: str


def _median_of(values: list[int]) -> Decimal:
    """Compute the median of a non-empty list of integers. Pure arithmetic, no LLM."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return Decimal(sorted_vals[mid])
    return (Decimal(sorted_vals[mid - 1]) + Decimal(sorted_vals[mid])) / Decimal(2)


def detect_volume_spikes(
    isin: str,
    bars: Sequence[DailyBar],
    volume_multiple: Decimal = Decimal("2.0"),
    return_threshold: Decimal = Decimal("0.03"),
) -> list[TechnicalSignalResult]:
    """Detect days when volume is >= `volume_multiple` times the prior-50-bar median
    AND the absolute 1-day price return is >= `return_threshold`.

    Returns VOLUME_SPIKE_UP when the return is positive, VOLUME_SPIKE_DOWN when negative.
    Requires at least 52 bars (51 prior bars for median + 1 prior close for return).
    Bars are processed in the order given; they must be sorted oldest-first.
    """
    results: list[TechnicalSignalResult] = []
    bar_list = list(bars)
    # Need index 51 onward: bar_list[i] with i >= 51 means i prior bars for median (50)
    # and bar_list[i-1] for prev_close. Window for median: bar_list[i-50 : i].
    for i in range(51, len(bar_list)):
        bar = bar_list[i]
        prev_bar = bar_list[i - 1]
        window_volumes = [b.volume for b in bar_list[i - 50 : i]]
        median_50d = _median_of(window_volumes)

        if bar.volume < volume_multiple * median_50d:
            continue

        if prev_bar.close == Decimal(0):
            continue

        return_1d = (bar.close - prev_bar.close) / prev_bar.close

        if abs(return_1d) < return_threshold:
            continue

        if return_1d > Decimal(0):
            signal_type = "volume_spike_up"
        else:
            signal_type = "volume_spike_down"

        results.append(
            TechnicalSignalResult(
                isin=isin,
                signal_type=signal_type,
                signal_date=bar.date,
                close_price=bar.close,
                volume=bar.volume,
                volume_median_50d=median_50d,
                price_return_1d=return_1d,
                consolidation_start=None,
                consolidation_high=None,
                consolidation_low=None,
                ath_since=None,
                adjustment_factor=bar.adjustment_factor,
                rule_version=RULE_VERSION,
            )
        )
    return results


def detect_consolidation_breakout(
    isin: str,
    bars: Sequence[DailyBar],
    lookback: int = 20,
    range_threshold: Decimal = Decimal("0.05"),
    breakout_threshold: Decimal = Decimal("0.02"),
) -> list[TechnicalSignalResult]:
    """Detect consolidation breakouts and breakdowns.

    Slides a window of `lookback` bars. A consolidation is detected when
    (window_high - window_low) / window_midpoint <= range_threshold.
    The bar immediately after the window is checked:
    - close >= (1 + breakout_threshold) * window_high -> CONSOLIDATION_BREAKOUT
    - close <= (1 - breakout_threshold) * window_low  -> CONSOLIDATION_BREAKDOWN

    Bars must be sorted oldest-first. Requires at least lookback+1 bars.
    """
    results: list[TechnicalSignalResult] = []
    bar_list = list(bars)

    for i in range(lookback, len(bar_list)):
        window = bar_list[i - lookback : i]
        next_bar = bar_list[i]

        window_high = max(b.close for b in window)
        window_low = min(b.close for b in window)
        window_midpoint = (window_high + window_low) / Decimal(2)

        if window_midpoint == Decimal(0):
            continue

        price_range_ratio = (window_high - window_low) / window_midpoint
        if price_range_ratio > range_threshold:
            continue

        # Consolidation detected. Check the next bar.
        breakout_level = (Decimal(1) + breakout_threshold) * window_high
        breakdown_level = (Decimal(1) - breakout_threshold) * window_low

        if next_bar.close >= breakout_level:
            signal_type = "consolidation_breakout"
        elif next_bar.close <= breakdown_level:
            signal_type = "consolidation_breakdown"
        else:
            continue

        results.append(
            TechnicalSignalResult(
                isin=isin,
                signal_type=signal_type,
                signal_date=next_bar.date,
                close_price=next_bar.close,
                volume=next_bar.volume,
                volume_median_50d=None,
                price_return_1d=None,
                consolidation_start=window[0].date,
                consolidation_high=window_high,
                consolidation_low=window_low,
                ath_since=None,
                adjustment_factor=next_bar.adjustment_factor,
                rule_version=RULE_VERSION,
            )
        )
    return results


def detect_ath_breakout(
    isin: str,
    bars: Sequence[DailyBar],
    min_history_bars: int = 252,
) -> list[TechnicalSignalResult]:
    """Detect all-time-high breakouts: the close exceeds all prior closes.

    Requires strictly more than `min_history_bars` bars before it starts
    checking (so bar index min_history_bars+1 onward). The prior maximum
    uses all bars before the current one. `ath_since` is the date of the
    first bar in the series.

    A close exactly equal to the prior maximum is NOT a breakout (must be
    strictly greater).

    Bars must be sorted oldest-first.
    """
    results: list[TechnicalSignalResult] = []
    bar_list = list(bars)

    if len(bar_list) < min_history_bars + 2:
        return results

    ath_since = bar_list[0].date

    for i in range(min_history_bars + 1, len(bar_list)):
        bar = bar_list[i]
        prior_max = max(b.close for b in bar_list[:i])

        if bar.close > prior_max:
            results.append(
                TechnicalSignalResult(
                    isin=isin,
                    signal_type="ath_breakout",
                    signal_date=bar.date,
                    close_price=bar.close,
                    volume=bar.volume,
                    volume_median_50d=None,
                    price_return_1d=None,
                    consolidation_start=None,
                    consolidation_high=None,
                    consolidation_low=None,
                    ath_since=ath_since,
                    adjustment_factor=bar.adjustment_factor,
                    rule_version=RULE_VERSION,
                )
            )
    return results
