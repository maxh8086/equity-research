"""Screener schedules schema and exceptions."""

from __future__ import annotations


class SchedulesParsingError(Exception):
    """Parsing failure on Screener schedules response."""


# The raw API response is dict[str, dict[str, str]]:
# {
#   "Land": {"Mar 2015": "565", "Mar 2016": "348", ...},
#   "Building": {"Mar 2015": "6,508", ...},
#   "Gross Block": {"Mar 2015": "...", ...},
#   "Accumulated Depreciation": {...},
#   ...
# }
#
# Values are strings with thousands commas, in rupees crore.
# Period labels are "Mar YYYY" strings.
