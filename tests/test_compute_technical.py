"""Tests for core.compute.technical — pure function tests with no I/O.

All arithmetic uses Decimal. Tests follow CLAUDE.md R1: no floats, no LLM.
"""

from datetime import date
from decimal import Decimal

import pytest

from core.compute.technical import (
    RULE_VERSION,
    DailyBar,
    TechnicalSignalResult,
    detect_ath_breakout,
    detect_consolidation_breakout,
    detect_volume_spikes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ADJ = Decimal("1")
_ISIN = "INE848E01016"


def _bar(d: date, close: Decimal | int | str, volume: int = 1_000_000) -> DailyBar:
    return DailyBar(
        date=d,
        close=Decimal(str(close)),
        volume=volume,
        adjustment_factor=_ADJ,
    )


def _make_bars(
    n: int,
    close: Decimal | int | str = 100,
    volume: int = 1_000_000,
    start: date = date(2022, 1, 1),
) -> list[DailyBar]:
    """Generate `n` flat bars with the same close and volume, one per trading day."""
    from datetime import timedelta

    bars: list[DailyBar] = []
    d = start
    for _ in range(n):
        bars.append(_bar(d, close, volume))
        d += timedelta(days=1)
    return bars


# ---------------------------------------------------------------------------
# Volume spike detection
# ---------------------------------------------------------------------------


class TestVolumeSpikes:
    """Unit tests for detect_volume_spikes."""

    def _bars_for_spike(
        self,
        close: Decimal = Decimal("100"),
        spike_return: Decimal = Decimal("0.05"),
        spike_volume_multiple: Decimal = Decimal("3"),
        n_baseline: int = 55,
    ) -> tuple[list[DailyBar], int]:
        """Return bars where bar at index 52 (0-based) is a spike day."""
        from datetime import timedelta

        baseline_vol = 1_000_000
        spike_vol = int(baseline_vol * spike_volume_multiple)

        bars: list[DailyBar] = []
        d = date(2022, 1, 1)

        # 52 baseline bars
        for i in range(52):
            bars.append(_bar(d, close, baseline_vol))
            d += timedelta(days=1)

        # Spike bar: prev_close = 100, close = 100 * (1 + return)
        spike_close = close * (Decimal(1) + spike_return)
        bars.append(_bar(d, spike_close, spike_vol))
        spike_index = len(bars) - 1
        d += timedelta(days=1)

        # Any extra bars
        for _ in range(n_baseline - 52 - 1):
            bars.append(_bar(d, close, baseline_vol))
            d += timedelta(days=1)

        return bars, spike_index

    def test_3x_volume_5pct_return_up_is_spike_up(self) -> None:
        bars, spike_idx = self._bars_for_spike(
            spike_return=Decimal("0.05"),
            spike_volume_multiple=Decimal("3"),
        )
        results = detect_volume_spikes(_ISIN, bars)
        assert len(results) == 1
        sig = results[0]
        assert sig.signal_type == "volume_spike_up"
        assert sig.signal_date == bars[spike_idx].date
        assert sig.close_price == bars[spike_idx].close
        assert sig.volume == bars[spike_idx].volume
        assert sig.volume_median_50d is not None
        assert sig.price_return_1d is not None
        # Return is approximately 0.05
        assert abs(sig.price_return_1d - Decimal("0.05")) < Decimal("0.001")
        assert sig.rule_version == RULE_VERSION

    def test_3x_volume_5pct_return_down_is_spike_down(self) -> None:
        bars, _ = self._bars_for_spike(
            spike_return=Decimal("-0.05"),
            spike_volume_multiple=Decimal("3"),
        )
        results = detect_volume_spikes(_ISIN, bars)
        assert len(results) == 1
        assert results[0].signal_type == "volume_spike_down"

    def test_1_5x_volume_is_not_a_spike(self) -> None:
        """Volume 1.5x median but return 5%: no spike because volume threshold not met."""
        bars, _ = self._bars_for_spike(
            spike_return=Decimal("0.05"),
            spike_volume_multiple=Decimal("1.5"),
        )
        results = detect_volume_spikes(_ISIN, bars)
        assert results == []

    def test_2x_volume_2pct_return_just_at_threshold(self) -> None:
        """Exactly at the defaults: 2x volume and 3% return should fire."""
        bars, _ = self._bars_for_spike(
            spike_return=Decimal("0.03"),
            spike_volume_multiple=Decimal("2.0"),
        )
        results = detect_volume_spikes(_ISIN, bars)
        assert len(results) == 1

    def test_2x_volume_1pct_return_below_return_threshold(self) -> None:
        """Volume threshold met but return only 1%: no spike."""
        bars, _ = self._bars_for_spike(
            spike_return=Decimal("0.01"),
            spike_volume_multiple=Decimal("3.0"),
        )
        results = detect_volume_spikes(_ISIN, bars)
        assert results == []

    def test_fewer_than_52_bars_returns_empty(self) -> None:
        bars = _make_bars(51)
        assert detect_volume_spikes(_ISIN, bars) == []

    def test_empty_bars_returns_empty(self) -> None:
        assert detect_volume_spikes(_ISIN, []) == []

    def test_isin_is_propagated(self) -> None:
        bars, _ = self._bars_for_spike()
        results = detect_volume_spikes(_ISIN, bars)
        for sig in results:
            assert sig.isin == _ISIN


# ---------------------------------------------------------------------------
# Consolidation breakout / breakdown detection
# ---------------------------------------------------------------------------


class TestConsolidationBreakout:
    """Unit tests for detect_consolidation_breakout."""

    def _tight_range_bars(
        self,
        n: int = 25,
        center: Decimal = Decimal("100"),
        half_range: Decimal = Decimal("1"),  # 2% range, well within 5% default
    ) -> list[DailyBar]:
        """Alternating high/low bars in a tight range."""
        from datetime import timedelta

        bars: list[DailyBar] = []
        d = date(2022, 1, 1)
        for i in range(n):
            close = center + half_range if i % 2 == 0 else center - half_range
            bars.append(_bar(d, close))
            d += timedelta(days=1)
        return bars

    def test_tight_range_then_breakout(self) -> None:
        """20 bars in a tight range, 21st bar closes above (window_high * 1.02)."""
        from datetime import timedelta

        bars = self._tight_range_bars(n=20)  # 20-bar window
        # Window high = 101, breakout needs close >= 101 * 1.02 = 103.02
        breakout_close = Decimal("104")
        d = bars[-1].date + timedelta(days=1)
        bars.append(_bar(d, breakout_close))

        results = detect_consolidation_breakout(_ISIN, bars)
        assert len(results) >= 1
        # The last bar triggers a breakout
        last_sig = next((s for s in results if s.signal_date == d), None)
        assert last_sig is not None
        assert last_sig.signal_type == "consolidation_breakout"
        assert last_sig.consolidation_start == bars[0].date
        assert last_sig.consolidation_high == Decimal("101")
        assert last_sig.consolidation_low == Decimal("99")
        assert last_sig.rule_version == RULE_VERSION

    def test_tight_range_then_breakdown(self) -> None:
        from datetime import timedelta

        bars = self._tight_range_bars(n=20)
        # Window low = 99, breakdown needs close <= 99 * (1 - 0.02) = 97.02
        breakdown_close = Decimal("96")
        d = bars[-1].date + timedelta(days=1)
        bars.append(_bar(d, breakdown_close))

        results = detect_consolidation_breakout(_ISIN, bars)
        last_sig = next((s for s in results if s.signal_date == d), None)
        assert last_sig is not None
        assert last_sig.signal_type == "consolidation_breakdown"

    def test_wide_range_no_signal(self) -> None:
        """A 10% range (above the 5% threshold) should not trigger."""
        from datetime import timedelta

        bars: list[DailyBar] = []
        d = date(2022, 1, 1)
        for i in range(20):
            close = Decimal("110") if i % 2 == 0 else Decimal("90")  # 20% range
            bars.append(_bar(d, close))
            d += timedelta(days=1)
        bars.append(_bar(d, Decimal("115")))  # would be a breakout if range were tight

        results = detect_consolidation_breakout(_ISIN, bars)
        assert results == []

    def test_fewer_than_lookback_plus_1_bars_returns_empty(self) -> None:
        bars = _make_bars(20)  # exactly lookback, no "next bar"
        assert detect_consolidation_breakout(_ISIN, bars) == []

    def test_isin_propagated(self) -> None:
        from datetime import timedelta

        bars = self._tight_range_bars(n=20)
        bars.append(_bar(bars[-1].date + timedelta(days=1), Decimal("105")))
        for sig in detect_consolidation_breakout(_ISIN, bars):
            assert sig.isin == _ISIN


# ---------------------------------------------------------------------------
# ATH breakout detection
# ---------------------------------------------------------------------------


class TestAthBreakout:
    """Unit tests for detect_ath_breakout."""

    def _ascending_bars(self, n: int, start_close: Decimal = Decimal("100")) -> list[DailyBar]:
        """Bars where each close is 1 higher than the prior, guaranteeing new highs."""
        from datetime import timedelta

        bars: list[DailyBar] = []
        d = date(2022, 1, 1)
        for i in range(n):
            bars.append(_bar(d, start_close + Decimal(i)))
            d += timedelta(days=1)
        return bars

    def test_new_close_high_produces_signal(self) -> None:
        """With 254 ascending bars, bar 253+ should produce ATH breakout signals."""
        bars = self._ascending_bars(260)
        results = detect_ath_breakout(_ISIN, bars)
        # Bars from index 253 onward should produce signals (min_history_bars=252)
        assert len(results) > 0
        for sig in results:
            assert sig.signal_type == "ath_breakout"
            assert sig.ath_since == bars[0].date
            assert sig.rule_version == RULE_VERSION

    def test_equal_to_prior_high_is_not_breakout(self) -> None:
        """A bar exactly matching the prior high should not produce a signal."""
        from datetime import timedelta

        # 253 identical bars, then one more at the same level
        bars = _make_bars(253, close=100)
        d = bars[-1].date + timedelta(days=1)
        bars.append(_bar(d, 100))

        results = detect_ath_breakout(_ISIN, bars)
        assert results == []

    def test_fewer_than_min_history_plus_1_returns_empty(self) -> None:
        """Not enough bars: no signals."""
        bars = _make_bars(253)  # exactly min_history_bars + 1, but need min_history_bars + 2
        # With 253 bars the loop starts at index 253 which does not exist
        assert detect_ath_breakout(_ISIN, bars) == []

    def test_descending_bars_never_signal(self) -> None:
        """If the close is always falling, no ATH breakout occurs."""
        from datetime import timedelta

        bars: list[DailyBar] = []
        d = date(2022, 1, 1)
        for i in range(260):
            bars.append(_bar(d, Decimal(300) - Decimal(i)))
            d += timedelta(days=1)
        assert detect_ath_breakout(_ISIN, bars) == []

    def test_isin_propagated(self) -> None:
        bars = self._ascending_bars(260)
        for sig in detect_ath_breakout(_ISIN, bars):
            assert sig.isin == _ISIN


# ---------------------------------------------------------------------------
# Property: future bars never change past signals
# ---------------------------------------------------------------------------


class TestPropertyFutureDoesNotChangePast:
    """Adding bars beyond the current date must not alter signals already detected."""

    def test_volume_spikes_stable(self) -> None:
        from datetime import timedelta

        bars, spike_idx = _make_and_spike()
        results_before = detect_volume_spikes(_ISIN, bars)

        # Add 10 more bars
        d = bars[-1].date
        extra: list[DailyBar] = []
        for i in range(10):
            d += timedelta(days=1)
            extra.append(_bar(d, Decimal("100"), 1_000_000))
        results_after = detect_volume_spikes(_ISIN, bars + extra)

        # Signals produced from the original slice must be identical
        original_dates = {s.signal_date for s in results_before}
        for sig in results_after:
            if sig.signal_date in original_dates:
                # Find the matching signal in results_before
                match = next(s for s in results_before if s.signal_date == sig.signal_date)
                assert sig == match

    def test_ath_signals_stable(self) -> None:
        from datetime import timedelta

        bars = _make_bars(260, close=100)
        # The last few bars are all the same, no ATH. Make ascending for last part.
        ascending_suffix: list[DailyBar] = []
        d = bars[-1].date
        for i in range(10):
            d += timedelta(days=1)
            ascending_suffix.append(_bar(d, Decimal(101 + i)))
        full_bars = bars + ascending_suffix

        results_before = detect_ath_breakout(_ISIN, full_bars)

        # Add 5 more ascending bars
        extra: list[DailyBar] = []
        for i in range(5):
            d += timedelta(days=1)
            extra.append(_bar(d, Decimal(111 + i)))
        results_after = detect_ath_breakout(_ISIN, full_bars + extra)

        original_dates = {s.signal_date for s in results_before}
        for sig in results_after:
            if sig.signal_date in original_dates:
                match = next(s for s in results_before if s.signal_date == sig.signal_date)
                assert sig == match


# ---------------------------------------------------------------------------
# Property: multiplying all closes by a constant must not change signal counts
# ---------------------------------------------------------------------------


class TestPropertyScaleInvariance:
    """Multiplying all closes by the same positive factor must not create or remove signals."""

    def test_volume_spike_scale_invariant(self) -> None:
        bars, _ = _make_and_spike()
        results_1x = detect_volume_spikes(_ISIN, bars)

        # Scale closes by 2x
        scaled = [
            DailyBar(
                date=b.date,
                close=b.close * Decimal("2"),
                volume=b.volume,
                adjustment_factor=b.adjustment_factor,
            )
            for b in bars
        ]
        results_2x = detect_volume_spikes(_ISIN, scaled)

        assert len(results_1x) == len(results_2x)
        for s1, s2 in zip(results_1x, results_2x):
            assert s1.signal_type == s2.signal_type
            assert s1.signal_date == s2.signal_date

    def test_consolidation_scale_invariant(self) -> None:
        from datetime import timedelta

        # Build a tight-range consolidation + breakout
        d = date(2022, 1, 1)
        bars: list[DailyBar] = []
        for i in range(20):
            close = Decimal("101") if i % 2 == 0 else Decimal("99")
            bars.append(_bar(d + __import__("datetime").timedelta(days=i), close))
        bars.append(
            _bar(bars[-1].date + timedelta(days=1), Decimal("105"))
        )

        results_1x = detect_consolidation_breakout(_ISIN, bars)

        scaled = [
            DailyBar(
                date=b.date,
                close=b.close * Decimal("3"),
                volume=b.volume,
                adjustment_factor=b.adjustment_factor,
            )
            for b in bars
        ]
        results_3x = detect_consolidation_breakout(_ISIN, scaled)

        assert len(results_1x) == len(results_3x)
        for s1, s2 in zip(results_1x, results_3x):
            assert s1.signal_type == s2.signal_type
            assert s1.signal_date == s2.signal_date

    def test_ath_scale_invariant(self) -> None:
        bars = _make_bars(260, close=100)
        # ATH breakout only if a bar exceeds the prior high; with all flat no signals
        assert detect_ath_breakout(_ISIN, bars) == []

        # Ascending for a real ATH test
        bars_asc = [
            DailyBar(
                date=b.date,
                close=Decimal(100 + i),
                volume=b.volume,
                adjustment_factor=b.adjustment_factor,
            )
            for i, b in enumerate(bars)
        ]
        results_1x = detect_ath_breakout(_ISIN, bars_asc)

        scaled = [
            DailyBar(
                date=b.date,
                close=b.close * Decimal("5"),
                volume=b.volume,
                adjustment_factor=b.adjustment_factor,
            )
            for b in bars_asc
        ]
        results_5x = detect_ath_breakout(_ISIN, scaled)

        assert len(results_1x) == len(results_5x)


# ---------------------------------------------------------------------------
# Shared helper used by property tests
# ---------------------------------------------------------------------------


def _make_and_spike() -> tuple[list[DailyBar], int]:
    """Build a minimal bar list that contains exactly one volume spike."""
    from datetime import timedelta

    baseline_close = Decimal("100")
    baseline_vol = 1_000_000
    spike_vol = 3_500_000  # 3.5x median

    bars: list[DailyBar] = []
    d = date(2022, 1, 1)
    for _ in range(52):
        bars.append(_bar(d, baseline_close, baseline_vol))
        d += timedelta(days=1)

    # Bar 52 (index 52): big volume, +5% return
    spike_close = baseline_close * Decimal("1.05")
    bars.append(_bar(d, spike_close, spike_vol))
    spike_idx = len(bars) - 1

    # A few more flat bars
    for _ in range(5):
        d += timedelta(days=1)
        bars.append(_bar(d, baseline_close, baseline_vol))

    return bars, spike_idx
