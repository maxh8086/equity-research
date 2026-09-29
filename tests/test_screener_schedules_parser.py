"""Tests for Screener schedules parser (tests first, R4).

Tests the parser logic for extracting gross block facts from Screener's API response.
The API response is dict[str, dict[str, str]] with values in rupees crore, strings with commas.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from ingest.screener_schedules.parser import (
    parse_schedules,
    SchedulesParsingError,
)


# Synthetic fixture (no real API data in git per CLAUDE.md)
FIXTURE_TCS_SCHEDULES = {
    "Land": {
        "Mar 2015": "565",
        "Mar 2024": "1,294",
        "Mar 2025": "2,873",
    },
    "Building": {
        "Mar 2015": "6,508",
        "Mar 2024": "19,314",
        "Mar 2025": "19,681",
    },
    "Gross Block": {
        "Mar 2015": "123,456",
        "Mar 2024": "234,567",
        "Mar 2025": "345,678",
    },
    "Accumulated Depreciation": {
        "Mar 2015": "50,000",
        "Mar 2024": "100,000",
        "Mar 2025": "120,000",
    },
}


class TestParseSchedules:
    """Test parser on synthetic Screener API responses."""

    def test_parse_gross_block_every_period(self):
        """Parse Gross Block for every period column, oldest first."""
        raw = json.dumps(FIXTURE_TCS_SCHEDULES).encode()
        facts = parse_schedules(
            raw,
            symbol="TCS",
            isin="INE467B01029",
            consolidation="consolidated",
        )

        assert [f.period_end for f in facts] == [
            date(2015, 3, 31),
            date(2024, 3, 31),
            date(2025, 3, 31),
        ]
        assert [f.value for f in facts] == [
            Decimal("1234560000000"),
            Decimal("2345670000000"),
            Decimal("3456780000000"),
        ]
        assert {f.line_item for f in facts} == {"property_plant_and_equipment_gross"}
        gross_fact = facts[-1]
        # 345,678 crore = 345678 * 10^7 = 3456780000000
        assert gross_fact.value == Decimal("3456780000000")
        assert gross_fact.period_end == date(2025, 3, 31)
        assert gross_fact.unit == "INR"

    def test_parse_comma_separated_crore(self):
        """Parse crore strings with thousands commas correctly."""
        data = {
            "Gross Block": {
                "Mar 2024": "1,234,567",  # 1.2 billion crore
            }
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        assert facts
        gross_fact = facts[0]
        # 1,234,567 crore = 1234567 * 10^7
        assert gross_fact.value == Decimal("12345670000000")

    def test_missing_gross_block_key(self):
        """Handle response without Gross Block key."""
        data = {
            "Land": {"Mar 2024": "100"},
            "Building": {"Mar 2024": "200"},
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        # No gross block = no facts
        assert facts == []

    def test_empty_gross_block(self):
        """Handle empty Gross Block dict."""
        data = {
            "Gross Block": {},
            "Land": {"Mar 2024": "100"},
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        # Empty periods = no facts
        assert facts == []

    def test_invalid_json(self):
        """Raise on invalid JSON."""
        raw = b"not json"
        with pytest.raises(SchedulesParsingError, match="invalid JSON"):
            parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

    def test_invalid_period_label(self):
        """Skip periods with invalid date labels."""
        data = {
            "Gross Block": {
                "Mar 2024": "100",
                "Invalid Date": "200",
                "Mar 2025": "300",
            }
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        # The invalid label is skipped; both valid periods are kept.
        assert [f.period_end for f in facts] == [date(2024, 3, 31), date(2025, 3, 31)]
        assert facts[1].value == Decimal("3000000000")

    def test_non_numeric_value(self):
        """Skip non-numeric Gross Block values."""
        data = {
            "Gross Block": {
                "Mar 2024": "ABC",
                "Mar 2025": "123,456",
            }
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        # The non-numeric column is skipped, the numeric one kept.
        assert len(facts) == 1
        assert facts[0].period_end == date(2025, 3, 31)

    def test_period_end_date_calculation(self):
        """Calculate period_end correctly as March 31 of the year."""
        data = {
            "Gross Block": {
                "Mar 2020": "100",
                "Mar 2021": "200",
            }
        }
        raw = json.dumps(data).encode()
        facts = parse_schedules(raw, symbol="TEST", isin="INE000000000", consolidation="standalone")

        assert [f.period_end for f in facts] == [date(2020, 3, 31), date(2021, 3, 31)]
