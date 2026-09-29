"""Tests for report_cli.py: argument parsing, date parsing, and error handling."""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

import report_cli
from core.compute.guidance_report import GuidanceReport
from core.compute.guidance_resolution import DeliveryRate
from core.timezones import IST


class TestDateParsing:
    """Test parsing of --as-of date strings to timezone-aware datetime."""

    def test_as_of_naive_to_aware(self) -> None:
        """Naive date YYYY-MM-DD becomes timezone-aware IST datetime at start of day."""
        result = report_cli._datetime("2026-03-31")
        assert result == datetime(2026, 3, 31, 0, 0, 0, tzinfo=IST)
        assert result.tzinfo is not None
        assert result.utcoffset() is not None

    def test_date_parsing(self) -> None:
        """Parse YYYY-MM-DD to date object."""
        result = report_cli._date("2026-03-31")
        assert result == date(2026, 3, 31)

    def test_invalid_date_format(self) -> None:
        """Reject invalid date format."""
        with pytest.raises(ValueError):
            report_cli._date("31-03-2026")

    def test_invalid_datetime_format(self) -> None:
        """Reject invalid datetime format."""
        with pytest.raises(ValueError):
            report_cli._datetime("31-03-2026")


class TestArgumentParsing:
    """Test CLI argument parsing."""

    def test_required_arguments(self) -> None:
        """--symbol and --as-of are required."""
        with pytest.raises(SystemExit):
            report_cli.main([])
        with pytest.raises(SystemExit):
            report_cli.main(["--symbol", "RELIANCE"])
        with pytest.raises(SystemExit):
            report_cli.main(["--as-of", "2026-03-31"])

    def test_symbol_and_as_of(self) -> None:
        """Parse --symbol and --as-of successfully."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = _fake_report()

                code = report_cli.main(["--symbol", "RELIANCE", "--as-of", "2026-03-31"])
                assert code == 0
                mock_symbol_to_isin.assert_called_once()

    def test_optional_quarter_start(self) -> None:
        """Parse optional --quarter-start."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = _fake_report()

                code = report_cli.main(
                    ["--symbol", "RELIANCE", "--as-of", "2026-03-31", "--quarter-start", "2025-10-01"]
                )
                assert code == 0
                # Check that quarter_start was passed correctly
                call_kwargs = mock_guidance.call_args[1]
                assert call_kwargs["quarter_start"] == date(2025, 10, 1)


class TestDefaultQuarterStart:
    """Test automatic quarter start calculation."""

    @pytest.mark.parametrize(
        "as_of_date,expected_quarter",
        [
            ("2026-01-15", date(2026, 1, 1)),  # January -> Q4 (Jan 1)
            ("2026-03-31", date(2026, 1, 1)),  # March -> Q4 (Jan 1)
            ("2026-04-01", date(2026, 4, 1)),  # April -> Q1 (Apr 1)
            ("2026-06-30", date(2026, 4, 1)),  # June -> Q1 (Apr 1)
            ("2026-07-01", date(2026, 7, 1)),  # July -> Q2 (Jul 1)
            ("2026-09-30", date(2026, 7, 1)),  # September -> Q2 (Jul 1)
            ("2026-10-01", date(2026, 10, 1)),  # October -> Q3 (Oct 1)
            ("2026-12-31", date(2026, 10, 1)),  # December -> Q3 (Oct 1)
        ],
    )
    def test_quarter_start_calculation(self, as_of_date: str, expected_quarter: date) -> None:
        """Calculate quarter start from as_of date when --quarter-start is not provided."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = _fake_report()

                code = report_cli.main(["--symbol", "RELIANCE", "--as-of", as_of_date])
                assert code == 0
                call_kwargs = mock_guidance.call_args[1]
                assert call_kwargs["quarter_start"] == expected_quarter


class TestSymbolResolution:
    """Test symbol-to-ISIN resolution."""

    def test_symbol_not_found(self) -> None:
        """Unknown symbol returns exit code 1 with error message."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("report_cli.create_engine"):
                mock_symbol_to_isin.return_value = None

                code = report_cli.main(["--symbol", "UNKNOWN", "--as-of", "2026-03-31"])
                assert code == 1

    def test_symbol_resolution_call(self) -> None:
        """symbol_to_isin_as_of is called with correct parameters."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = _fake_report()

                report_cli.main(["--symbol", "RELIANCE", "--as-of", "2026-03-31"])

                # Check that symbol_to_isin_as_of was called with the right parameters
                call_kwargs = mock_symbol_to_isin.call_args[1]
                assert call_kwargs["symbol"] == "RELIANCE"
                assert call_kwargs["on"] == date(2026, 3, 31)
                assert call_kwargs["as_of"] == datetime(2026, 3, 31, 0, 0, 0, tzinfo=IST)


class TestGuidanceReportCall:
    """Test guidance_report_as_of function call."""

    def test_guidance_report_call(self) -> None:
        """guidance_report_as_of is called with correct parameters."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = _fake_report()

                report_cli.main(["--symbol", "RELIANCE", "--as-of", "2026-03-31", "--quarter-start", "2025-10-01"])

                # Check that guidance_report_as_of was called with correct parameters
                call_args = mock_guidance.call_args
                assert call_args[1]["isin"] == "INE002A01018"
                assert call_args[1]["symbol"] == "RELIANCE"
                assert call_args[1]["as_of"] == datetime(2026, 3, 31, 0, 0, 0, tzinfo=IST)
                assert call_args[1]["quarter_start"] == date(2025, 10, 1)


class TestOutput:
    """Test output rendering."""

    def test_markdown_output(self, capsys) -> None:
        """Renders and prints guidance report as markdown."""
        with patch("report_cli.symbol_to_isin_as_of") as mock_symbol_to_isin:
            with patch("core.db.pit.guidance_report_as_of") as mock_guidance:
                fake_report = _fake_report()
                mock_symbol_to_isin.return_value = "INE002A01018"
                mock_guidance.return_value = fake_report

                code = report_cli.main(["--symbol", "RELIANCE", "--as-of", "2026-03-31"])

                assert code == 0
                captured = capsys.readouterr()
                # Should contain markdown header
                assert "# RELIANCE guidance report" in captured.out
                assert "RELIANCE:" in captured.out


def _fake_report() -> GuidanceReport:
    """Create a minimal fake GuidanceReport for testing."""
    from core.compute.guidance_report import SourceRef

    return GuidanceReport(
        isin="INE002A01018",
        symbol="RELIANCE",
        as_of=datetime(2026, 3, 31, 0, 0, 0, tzinfo=IST),
        quarter_start=date(2025, 10, 1),
        headline="RELIANCE: no guidance on record as of 2026-03-31.",
        rows=(),
        new_this_quarter=(),
        silent=(),
        delivery=DeliveryRate(rate=None, met=0, missed=0, open=0, unresolvable=0, silent=0),
        sources=(),
    )
