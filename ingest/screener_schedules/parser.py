"""Parser for Screener company schedules (fixed assets schedule).

Extracts gross property, plant & equipment from the API response.
Values are strings with thousands commas, in rupees crore (multiply by 10^7 for rupees).
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from ingest.screener_schedules.schema import SchedulesParsingError


RULE_VERSION = "screener_schedules/1"


@dataclass(frozen=True)
class ParsedFact:
    """One extracted financial fact ready for financial_facts table."""

    line_item: str  # e.g., "property_plant_and_equipment_gross"
    period_end: date
    value: Decimal  # in rupees (crore * 10^7)
    unit: str = "INR"


_MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
    )
}
_PERIOD_LABEL = re.compile(r"^([A-Z][a-z]{2}) (\d{4})$")
_CRORE = Decimal("10000000")


def _parse_crore_string(s: object) -> Decimal | None:
    """Parse a crore string with commas to Decimal rupees; None for anything else.

    "1,234,567" crore -> Decimal("12345670000000") rupees. Non-strings and
    non-finite numbers are rejected.
    """
    if not isinstance(s, str):
        return None
    text = s.replace(",", "").strip()
    if not text:
        return None
    try:
        crore = Decimal(text)
    except InvalidOperation:
        return None
    if not crore.is_finite():
        return None
    return crore * _CRORE


def _parse_period_end(label: object) -> date | None:
    """Parse "Mon YYYY" to the last day of that month ("Mar 2025" -> 2025-03-31).

    Anything that is not exactly a month label (e.g. "setAttributes") is None.
    """
    if not isinstance(label, str):
        return None
    match = _PERIOD_LABEL.match(label)
    if not match or match.group(1) not in _MONTHS:
        return None
    year, month = int(match.group(2)), _MONTHS[match.group(1)]
    try:
        return date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, calendar.IllegalMonthError):
        return None


def parse_schedules(
    raw_bytes: bytes,
    symbol: str,
    isin: str,
    consolidation: str,
) -> list[ParsedFact]:
    """Parse Screener schedules API response and extract facts.

    Args:
        raw_bytes: JSON response from /api/company/{id}/schedules/
        symbol: Company symbol (for logging/tracing)
        isin: Company ISIN (for context)
        consolidation: "standalone" or "consolidated"

    Returns:
        List of parsed facts ready for financial_facts table, or [] if no data.

    Raises:
        SchedulesParsingError: on JSON or parsing error.
    """
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SchedulesParsingError(f"invalid JSON: {e}") from e

    if not isinstance(data, dict):
        raise SchedulesParsingError(f"expected dict, got {type(data).__name__}")

    # Look for Gross Block
    if "Gross Block" not in data:
        return []

    gross_block = data["Gross Block"]
    if not isinstance(gross_block, dict) or not gross_block:
        return []

    # Find the latest period with a valid numeric value
    latest_fact: ParsedFact | None = None

    for period_label, value_str in gross_block.items():
        period_end = _parse_period_end(period_label)
        if not period_end:
            continue

        rupees = _parse_crore_string(value_str)
        if rupees is None:
            continue

        # Keep the latest period by date
        if latest_fact is None or period_end > latest_fact.period_end:
            latest_fact = ParsedFact(
                line_item="property_plant_and_equipment_gross",
                period_end=period_end,
                value=rupees,
                unit="INR",
            )

    return [latest_fact] if latest_fact else []
