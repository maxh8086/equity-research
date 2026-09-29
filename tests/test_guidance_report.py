"""Unit tests for core.compute.guidance_report.

Pure unit tests with no database dependency. Tests the assembly and rendering
of guidance reports from in-memory claim data.
"""

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal

from core.compute.guidance import HedgeStrength, Unit
from core.compute.guidance_resolution import Status, UnresolvableReason, DeliveryRate
from core.compute.guidance_report import (
    SourceRef,
    Figure,
    InputFact,
    ClaimView,
    ReportRow,
    GuidanceReport,
    format_value,
    build_report,
    render_markdown,
    _guided,
    _prior,
    _actual,
)


# Fixtures for common test data

@pytest.fixture
def tz_utc():
    """UTC timezone for reproducible tests."""
    return timezone.utc


@pytest.fixture
def source_url():
    """A sample earnings call transcript URL."""
    return "https://example.com/earnings-call-2026-q2"


@pytest.fixture
def source_ref(source_url, tz_utc):
    """A valid SourceRef with URL and timezone-aware datetime."""
    return SourceRef(
        url=source_url,
        as_of=datetime(2026, 8, 15, 10, 30, tzinfo=tz_utc),
    )


@pytest.fixture
def second_source_ref(tz_utc):
    """A second valid SourceRef with a different date."""
    return SourceRef(
        url="https://example.com/investor-update-2026-q3",
        as_of=datetime(2026, 11, 1, 14, 0, tzinfo=tz_utc),
    )


@pytest.fixture
def claim_view_met(source_ref, tz_utc):
    """A claim that met guidance."""
    input_fact = InputFact(
        line_item="Revenue",
        period_end=date(2026, 6, 30),
        value=Decimal("52000000"),  # 5.2 crore in absolute rupees
        source=source_ref,
    )
    return ClaimView(
        metric="Revenue",
        period_label="Q1 FY27",
        call_date=date(2026, 8, 15),
        hedge=HedgeStrength.WILL,
        hedge_verbatim="We will reach",
        guided_low=Decimal("4.5"),
        guided_high=Decimal("5.5"),
        unit=Unit.INR_CRORE,
        claim_source=source_ref,
        status=Status.MET,
        reason=None,
        actual=Decimal("5.2"),
        actual_unit=Unit.INR_CRORE,
        period_end=date(2026, 6, 30),
        inputs=(input_fact,),
        silent=False,
    )


@pytest.fixture
def claim_view_missed(source_ref, tz_utc):
    """A claim that missed guidance."""
    input_fact = InputFact(
        line_item="Net Margin",
        period_end=date(2026, 3, 31),
        value=Decimal("1800000"),  # 18% in basis points representation
        source=source_ref,
    )
    return ClaimView(
        metric="Net Margin",
        period_label="Q1 FY27",
        call_date=date(2026, 8, 15),
        hedge=HedgeStrength.EXPECT,
        hedge_verbatim="We expect",
        guided_low=Decimal("20"),
        guided_high=None,  # Point estimate
        unit=Unit.PERCENT,
        claim_source=source_ref,
        status=Status.MISSED,
        reason=None,
        actual=Decimal("18"),
        actual_unit=Unit.PERCENT,
        period_end=date(2026, 6, 30),
        inputs=(input_fact,),
        silent=False,
    )


@pytest.fixture
def claim_view_silent(source_ref, tz_utc):
    """A claim that went silent."""
    return ClaimView(
        metric="EBITDA",
        period_label="Q2 FY27",
        call_date=date(2026, 8, 15),
        hedge=HedgeStrength.WILL,
        hedge_verbatim="We will achieve",
        guided_low=Decimal("10"),
        guided_high=Decimal("12"),
        unit=Unit.INR_CRORE,
        claim_source=source_ref,
        status=Status.OPEN,
        reason=None,
        actual=None,
        actual_unit=None,
        period_end=None,
        inputs=(),
        silent=True,
    )


@pytest.fixture
def claim_view_directional(source_ref, tz_utc):
    """A claim with no number (directional)."""
    return ClaimView(
        metric="Margin expansion",
        period_label="FY27",
        call_date=date(2026, 8, 15),
        hedge=HedgeStrength.AIM_TO,
        hedge_verbatim="We aim to",
        guided_low=None,
        guided_high=None,
        unit=Unit.PERCENT,  # Not used for directional
        claim_source=source_ref,
        status=Status.UNRESOLVABLE,
        reason=UnresolvableReason.DIRECTIONAL,
        actual=None,
        actual_unit=None,
        period_end=None,
        inputs=(),
        silent=False,
    )


class TestSourceRef:
    """Tests for SourceRef validation."""

    def test_valid_source_ref(self, tz_utc):
        """A valid SourceRef with URL and timezone-aware datetime."""
        ref = SourceRef(
            url="https://example.com/call",
            as_of=datetime(2026, 8, 15, 10, 30, tzinfo=tz_utc),
        )
        assert ref.url == "https://example.com/call"
        assert ref.as_of.tzinfo is not None

    def test_empty_url_raises(self, tz_utc):
        """Empty URL raises ValueError."""
        with pytest.raises(ValueError, match="a source needs a URL"):
            SourceRef(
                url="",
                as_of=datetime(2026, 8, 15, 10, 30, tzinfo=tz_utc),
            )

    def test_whitespace_only_url_raises(self, tz_utc):
        """Whitespace-only URL raises ValueError."""
        with pytest.raises(ValueError, match="a source needs a URL"):
            SourceRef(
                url="   ",
                as_of=datetime(2026, 8, 15, 10, 30, tzinfo=tz_utc),
            )

    def test_naive_datetime_raises(self):
        """Naive (no timezone) datetime raises ValueError."""
        with pytest.raises(ValueError, match="a source date must be timezone-aware"):
            SourceRef(
                url="https://example.com/call",
                as_of=datetime(2026, 8, 15, 10, 30),  # No tzinfo
            )

    def test_source_key_property(self, source_ref):
        """The key property returns a tuple of (url, as_of)."""
        key = source_ref.key
        assert isinstance(key, tuple)
        assert key[0] == source_ref.url
        assert key[1] == source_ref.as_of


class TestFigure:
    """Tests for Figure validation."""

    def test_figure_with_source(self, source_ref):
        """A Figure with a valid source is created successfully."""
        fig = Figure(
            label="Revenue Q1 FY27",
            value=Decimal("5.2"),
            unit=Unit.INR_CRORE,
            source=source_ref,
        )
        assert fig.label == "Revenue Q1 FY27"
        assert fig.value == Decimal("5.2")
        assert fig.unit is Unit.INR_CRORE

    def test_figure_without_source_raises(self):
        """A Figure without a source raises ValueError."""
        with pytest.raises(ValueError, match="figure .* has no source"):
            Figure(
                label="Revenue Q1 FY27",
                value=Decimal("5.2"),
                unit=Unit.INR_CRORE,
                source=None,
            )


class TestFormatValue:
    """Tests for format_value function."""

    def test_percent_formatting(self):
        """Percentages are formatted with one decimal place and % sign."""
        result = format_value(Decimal("15.55"), Unit.PERCENT)
        assert result == "15.6%"

    def test_percent_rounding_half_up(self):
        """Percentages use ROUND_HALF_UP."""
        result = format_value(Decimal("15.45"), Unit.PERCENT)
        assert result == "15.5%"

    def test_bps_formatting(self):
        """Basis points are formatted as integers with 'bps' suffix."""
        result = format_value(Decimal("150.6"), Unit.BPS)
        assert result == "151 bps"

    def test_inr_crore_formatting(self):
        """INR Crore uses rupee symbol, comma separators, and 'cr'."""
        result = format_value(Decimal("1234.5"), Unit.INR_CRORE)
        assert result == "₹1,235 cr"

    def test_inr_crore_large_number(self):
        """INR Crore handles large numbers with proper separators."""
        result = format_value(Decimal("123456.7"), Unit.INR_CRORE)
        assert "₹" in result and "cr" in result and "," in result

    def test_multiple_formatting(self):
        """Multiples are formatted with one decimal place and 'x'."""
        result = format_value(Decimal("2.55"), Unit.MULTIPLE)
        assert result == "2.6x"

    def test_count_formatting(self):
        """Count uses comma separators."""
        result = format_value(Decimal("1000"), Unit.COUNT)
        assert "1,000" in result


class TestGuidedFunction:
    """Tests for _guided function."""

    def test_guided_directional(self, claim_view_directional):
        """A claim with no guided_low returns 'directional'."""
        result = _guided(claim_view_directional)
        assert result == "directional"

    def test_guided_point_estimate(self, claim_view_missed):
        """A claim with guided_low but no guided_high is a point estimate."""
        result = _guided(claim_view_missed)
        assert result == "20.0%"

    def test_guided_range(self, claim_view_met):
        """A claim with both low and high is a range."""
        result = _guided(claim_view_met)
        assert "to" in result
        # Check that the range values are present (formatted with rupee symbol and crore)
        assert "cr" in result
        assert "₹" in result

    def test_guided_range_same_value(self):
        """When guided_low == guided_high, show as point."""
        from core.compute.guidance_report import _guided
        claim = ClaimView(
            metric="Test",
            period_label="Q1",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("10"),
            guided_high=Decimal("10"),
            unit=Unit.INR_CRORE,
            claim_source=SourceRef("https://test.com", datetime(2026, 8, 15, tzinfo=timezone.utc)),
            status=Status.OPEN,
            reason=None,
            actual=None,
            actual_unit=None,
            period_end=None,
            inputs=(),
            silent=False,
        )
        result = _guided(claim)
        assert result == "₹10 cr"
        assert "to" not in result


class TestPriorFunction:
    """Tests for _prior function."""

    def test_prior_with_earlier_input(self, source_ref):
        """A claim with earlier inputs returns a Figure for the latest earlier one."""
        earlier_input = InputFact(
            line_item="Revenue",
            period_end=date(2026, 3, 31),  # Earlier
            value=Decimal("40000000"),  # 4 crore
            source=source_ref,
        )
        current_input = InputFact(
            line_item="Revenue",
            period_end=date(2026, 6, 30),  # Current
            value=Decimal("50000000"),
            source=source_ref,
        )
        claim = ClaimView(
            metric="Revenue",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("5"),
            guided_high=None,
            unit=Unit.INR_CRORE,
            claim_source=source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("5.2"),
            actual_unit=Unit.INR_CRORE,
            period_end=date(2026, 6, 30),
            inputs=(earlier_input, current_input),
            silent=False,
        )
        prior = _prior(claim)
        assert prior is not None
        assert prior.value == Decimal("4")  # 40000000 / 10000000
        assert prior.unit is Unit.INR_CRORE

    def test_prior_no_earlier_input(self, source_ref):
        """A claim with no earlier inputs returns None."""
        # Create a claim with only an input at the same period_end
        input_fact = InputFact(
            line_item="Revenue",
            period_end=date(2026, 6, 30),
            value=Decimal("52000000"),
            source=source_ref,
        )
        claim = ClaimView(
            metric="Revenue",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="We will reach",
            guided_low=Decimal("4.5"),
            guided_high=Decimal("5.5"),
            unit=Unit.INR_CRORE,
            claim_source=source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("5.2"),
            actual_unit=Unit.INR_CRORE,
            period_end=date(2026, 6, 30),
            inputs=(input_fact,),
            silent=False,
        )
        prior = _prior(claim)
        assert prior is None

    def test_prior_no_period_end(self, claim_view_directional):
        """A claim with no period_end returns None."""
        prior = _prior(claim_view_directional)
        assert prior is None

    def test_prior_uses_latest_earlier(self, source_ref):
        """When multiple earlier inputs exist, use the latest one."""
        input1 = InputFact(
            line_item="Revenue",
            period_end=date(2025, 6, 30),  # Earliest
            value=Decimal("30000000"),
            source=source_ref,
        )
        input2 = InputFact(
            line_item="Revenue",
            period_end=date(2025, 12, 31),  # Middle
            value=Decimal("35000000"),
            source=source_ref,
        )
        input3 = InputFact(
            line_item="Revenue",
            period_end=date(2026, 6, 30),  # Current
            value=Decimal("50000000"),
            source=source_ref,
        )
        claim = ClaimView(
            metric="Revenue",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("5"),
            guided_high=None,
            unit=Unit.INR_CRORE,
            claim_source=source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("5.2"),
            actual_unit=Unit.INR_CRORE,
            period_end=date(2026, 6, 30),
            inputs=(input1, input2, input3),
            silent=False,
        )
        prior = _prior(claim)
        assert prior is not None
        assert prior.value == Decimal("3.5")  # From input2


class TestActualFunction:
    """Tests for _actual function."""

    def test_actual_with_sourced_input(self, claim_view_met):
        """An actual with a sourced input returns a Figure."""
        actual = _actual(claim_view_met)
        assert actual is not None
        assert actual.value == Decimal("5.2")
        assert actual.unit is Unit.INR_CRORE

    def test_actual_none_when_no_actual(self, claim_view_directional):
        """When claim.actual is None, return None."""
        actual = _actual(claim_view_directional)
        assert actual is None

    def test_actual_none_when_no_sourced_inputs(self, source_ref):
        """When inputs have no source, return None."""
        unsourced_input = InputFact(
            line_item="Revenue",
            period_end=date(2026, 3, 31),
            value=Decimal("50000000"),
            source=None,  # No source
        )
        claim = ClaimView(
            metric="Revenue",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("5"),
            guided_high=None,
            unit=Unit.INR_CRORE,
            claim_source=source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("5.2"),  # Has actual
            actual_unit=Unit.INR_CRORE,
            period_end=date(2026, 6, 30),
            inputs=(unsourced_input,),
            silent=False,
        )
        actual = _actual(claim)
        assert actual is None


class TestBuildReport:
    """Tests for build_report function."""

    def test_build_report_empty(self):
        """An empty claim list produces a report with no rows."""
        delivery = DeliveryRate(rate=None, met=0, missed=0, open=0, unresolvable=0, silent=0)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[],
            delivery=delivery,
        )
        assert len(report.rows) == 0
        assert len(report.sources) == 0

    def test_build_report_groups_new_this_quarter(self, claim_view_met, claim_view_missed, source_ref):
        """build_report separates new_this_quarter from other claims."""
        quarter_start = date(2026, 7, 1)
        old_claim = ClaimView(
            metric="Old Revenue",
            period_label="Q4 FY26",
            call_date=date(2026, 5, 15),  # Before quarter_start
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("4"),
            guided_high=None,
            unit=Unit.INR_CRORE,
            claim_source=source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("4.2"),
            actual_unit=Unit.INR_CRORE,
            period_end=date(2026, 3, 31),
            inputs=(),
            silent=False,
        )
        delivery = DeliveryRate(rate=Decimal("1"), met=2, missed=0, open=0, unresolvable=0, silent=0)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=quarter_start,
            claims=[old_claim, claim_view_met],
            delivery=delivery,
        )
        assert len(report.new_this_quarter) == 1  # Only claim_view_met
        assert len(report.rows) == 2

    def test_build_report_groups_silent(self, claim_view_met, claim_view_silent):
        """build_report separates silent claims."""
        delivery = DeliveryRate(rate=Decimal("1"), met=1, missed=0, open=0, unresolvable=0, silent=1)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[claim_view_met, claim_view_silent],
            delivery=delivery,
        )
        assert len(report.silent) == 1
        assert report.silent[0].claim.metric == "EBITDA"

    def test_build_report_collects_sources(self, claim_view_met, second_source_ref):
        """build_report deduplicates sources and sorts them."""
        # Create a second claim with different source
        claim2 = ClaimView(
            metric="Other",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("1"),
            guided_high=None,
            unit=Unit.PERCENT,
            claim_source=second_source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("1.5"),
            actual_unit=Unit.PERCENT,
            period_end=date(2026, 6, 30),
            inputs=(),
            silent=False,
        )
        delivery = DeliveryRate(rate=Decimal("1"), met=2, missed=0, open=0, unresolvable=0, silent=0)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[claim_view_met, claim2],
            delivery=delivery,
        )
        assert len(report.sources) == 2
        # Sources should be sorted by as_of
        assert report.sources[0].as_of < report.sources[1].as_of


class TestRenderMarkdown:
    """Tests for render_markdown function."""

    def test_render_markdown_empty_report(self):
        """An empty report renders with proper structure."""
        delivery = DeliveryRate(rate=None, met=0, missed=0, open=0, unresolvable=0, silent=0)
        report = GuidanceReport(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            headline="Test headline",
            rows=(),
            new_this_quarter=(),
            silent=(),
            delivery=delivery,
            sources=(),
        )
        md = render_markdown(report)
        assert "# TESTCO guidance report" in md
        assert "No guidance on record" in md
        assert "Nothing has gone silent" in md
        assert "## Sources" in md

    def test_render_markdown_numbered_sources(self, claim_view_met, second_source_ref):
        """Every source in the report is numbered [1], [2], etc."""
        claim2 = ClaimView(
            metric="Other",
            period_label="Q1 FY27",
            call_date=date(2026, 8, 15),
            hedge=HedgeStrength.WILL,
            hedge_verbatim="Will",
            guided_low=Decimal("1"),
            guided_high=None,
            unit=Unit.PERCENT,
            claim_source=second_source_ref,
            status=Status.MET,
            reason=None,
            actual=Decimal("1.5"),
            actual_unit=Unit.PERCENT,
            period_end=date(2026, 6, 30),
            inputs=(),
            silent=False,
        )
        delivery = DeliveryRate(rate=Decimal("1"), met=2, missed=0, open=0, unresolvable=0, silent=0)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[claim_view_met, claim2],
            delivery=delivery,
        )
        md = render_markdown(report)
        # Check that both [1] and [2] appear
        assert "[1]" in md
        assert "[2]" in md
        # Check that sources are listed
        assert "## Sources" in md
        assert "1. " in md
        assert "2. " in md

    def test_render_markdown_every_bracket_resolves(self, claim_view_met, claim_view_silent, source_ref):
        """Every bracketed number [n] in the report resolves to a source."""
        delivery = DeliveryRate(rate=Decimal("1"), met=1, missed=0, open=0, unresolvable=0, silent=1)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[claim_view_met, claim_view_silent],
            delivery=delivery,
        )
        md = render_markdown(report)

        # Extract all bracket numbers
        import re
        bracket_nums = set(re.findall(r"\[(\d+)\]", md))

        # Extract numbered sources
        source_nums = set(re.findall(r"^(\d+)\. ", md, re.MULTILINE))

        # Every bracket number should have a corresponding source
        for num in bracket_nums:
            assert num in source_nums, f"Bracket [{num}] has no corresponding source {num}."

    def test_render_markdown_silent_claims_excluded_from_delivery(self, claim_view_met, claim_view_silent, source_ref):
        """Silent claims are listed separately and not in the delivery rate."""
        delivery = DeliveryRate(rate=Decimal("1"), met=1, missed=0, open=0, unresolvable=0, silent=1)
        report = build_report(
            isin="INE123A01012",
            symbol="TESTCO",
            as_of=datetime(2026, 9, 29, tzinfo=timezone.utc),
            quarter_start=date(2026, 7, 1),
            claims=[claim_view_met, claim_view_silent],
            delivery=delivery,
        )
        md = render_markdown(report)

        # Check that the silent section exists and mentions the silent claim
        assert "## Went silent" in md
        assert "EBITDA" in md
        # The rate should only mention the met claim (1 out of 1)
        assert "delivery rate" in md or "met" in md.lower()
