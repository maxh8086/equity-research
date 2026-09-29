"""Real-shape quirks of the Screener schedules response (synthetic fixtures only).

The live response can carry non-period entries inside a row, such as
{"setAttributes": {"class": "strong"}} beside the period columns of 'Gross Block'.
"""

import json
from datetime import date
from decimal import Decimal

from ingest.screener_schedules.parser import parse_schedules


def _parse(data):
    return parse_schedules(
        json.dumps(data).encode(), symbol="T", isin="INE000000000", consolidation="consolidated"
    )


def test_set_attributes_dict_entry_is_ignored():
    data = {
        "Land": {"Mar 2025": "1,000", "setAttributes": {"class": "strong"}},
        "Gross Block": {
            "Mar 2024": "28,493",
            "Mar 2025": "29,404",
            "setAttributes": {"class": "strong"},
        },
    }
    facts = _parse(data)
    assert [f.period_end for f in facts] == [date(2024, 3, 31), date(2025, 3, 31)]
    assert facts[1].value == Decimal("294040000000")


def test_only_set_attributes_yields_no_facts():
    assert _parse({"Gross Block": {"setAttributes": {"class": "strong"}}}) == []


def test_non_string_values_are_ignored():
    facts = _parse({"Gross Block": {"Mar 2024": "100", "Mar 2025": 5, "Mar 2026": None, "Mar 2027": ["1"]}})
    assert [f.period_end for f in facts] == [date(2024, 3, 31)]


def test_non_period_string_keys_are_ignored():
    facts = _parse({"Gross Block": {"Mar 2024": "100", "class": "strong", "TTM": "9", "Mar": "9"}})
    assert [f.period_end for f in facts] == [date(2024, 3, 31)]


def test_other_months_use_last_day_of_month():
    facts = _parse({"Gross Block": {"Sep 2025": "10", "Feb 2024": "5"}})
    assert [f.period_end for f in facts] == [date(2024, 2, 29), date(2025, 9, 30)]


def test_leap_february_last_day():
    assert _parse({"Gross Block": {"Feb 2024": "5"}})[0].period_end == date(2024, 2, 29)


def test_non_finite_numbers_rejected():
    facts = _parse({"Gross Block": {"Mar 2024": "100", "Mar 2025": "NaN", "Mar 2026": "Infinity"}})
    assert [f.period_end for f in facts] == [date(2024, 3, 31)]


def test_crore_conversion_is_exact_decimal():
    assert _parse({"Gross Block": {"Mar 2025": "1,234.5678"}})[0].value == Decimal("12345678000")
