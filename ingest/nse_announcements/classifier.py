"""Hardcoded keyword rules for classifying NSE announcements.

Rule version: announcements_classify_v1

Strategy:
1. Match announcement type by keyword in subject/description
2. Extract event_date from the same text using hardcoded regexes
3. Return Classification with event_date, or None if date cannot be parsed
4. Unclassifiable announcements (no date extracted) are not written

CLAUDE.md R1: code computes, never LLM.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TypedDict

from core.db.models import ScheduledEventType
from ingest.nse_announcements.schema import AnnouncementRow

RULE_VERSION = "announcements_classify_v1"


class Classification(TypedDict, total=False):
    """Classification result: event type, date, and rule version."""

    event_type: ScheduledEventType
    event_date: date
    rule_version: str


# Hardcoded keyword patterns
BOARD_MEETING_KEYWORDS = frozenset({
    "board meeting",
    "board to meet",
    "board shall meet",
    "board will meet",
})

RESULTS_KEYWORDS = frozenset({
    "results",
    "quarterly results",
    "financial results",
    "results approved",
})

AGM_KEYWORDS = frozenset({
    "annual general meeting",
    "agm",
})

RECORD_DATE_KEYWORDS = frozenset({
    "record date",
})

EX_DIVIDEND_KEYWORDS = frozenset({
    "ex-dividend",
    "ex dividend",
})

BUYBACK_KEYWORDS = frozenset({
    "buyback",
    "share buyback",
    "buyback program",
    "buyback open",
    "buyback close",
})

# Date patterns (CLAUDE.md R1: hardcoded, not inferred)
# Matches: "15-Feb-2026", "31-Dec-2025", "28-Mar-2026", etc.
DATE_PATTERN_DMY = re.compile(
    r"(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-(\d{4})",
    re.IGNORECASE
)

# Month name to number
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _normalize_text(text: str) -> str:
    """Normalize for keyword matching."""
    return " ".join(text.lower().split())


def _extract_date_dmy(text: str) -> date | None:
    """Extract date in DD-MMM-YYYY format from text.

    Returns the first match found, or None.
    """
    match = DATE_PATTERN_DMY.search(text)
    if not match:
        return None

    day_str, month_str, year_str = match.groups()
    month_num = MONTH_MAP.get(month_str.lower())

    if month_num is None:
        return None

    try:
        return date(int(year_str), month_num, int(day_str))
    except ValueError:
        # Invalid date (e.g., Feb 30)
        return None


def classify_announcement(announcement: AnnouncementRow) -> Classification | None:
    """Classify an announcement by type and extract event date.

    Returns Classification with event_type and event_date, or None if:
    - Announcement type is unrecognized, or
    - Event date cannot be extracted from the text

    NEVER write a ScheduledEvent without a verified event_date.
    """
    subject_normalized = _normalize_text(announcement.subject)
    description_normalized = _normalize_text(announcement.description or "")
    combined_text = f"{announcement.subject} {announcement.description or ""}"

    event_type: ScheduledEventType | None = None

    # Determine event type by keywords
    if any(kw in subject_normalized or kw in description_normalized for kw in BOARD_MEETING_KEYWORDS):
        event_type = ScheduledEventType.BOARD_MEETING
    elif any(kw in subject_normalized or kw in description_normalized for kw in RESULTS_KEYWORDS):
        event_type = ScheduledEventType.RESULTS
    elif any(kw in subject_normalized or kw in description_normalized for kw in AGM_KEYWORDS):
        event_type = ScheduledEventType.AGM
    elif any(kw in subject_normalized or kw in description_normalized for kw in RECORD_DATE_KEYWORDS):
        event_type = ScheduledEventType.RECORD_DATE
    elif any(kw in subject_normalized or kw in description_normalized for kw in EX_DIVIDEND_KEYWORDS):
        event_type = ScheduledEventType.EX_DIVIDEND
    elif any(kw in subject_normalized or kw in description_normalized for kw in BUYBACK_KEYWORDS):
        # Default to BUYBACK_OPEN for generic buyback mentions
        event_type = ScheduledEventType.BUYBACK_OPEN

    # If no type matched, unclassifiable
    if event_type is None:
        return None

    # Extract event_date from announcement text
    event_date = _extract_date_dmy(combined_text)

    # If no date extracted, unclassifiable (do not write)
    if event_date is None:
        return None

    return Classification(
        event_type=event_type,
        event_date=event_date,
        rule_version=RULE_VERSION,
    )
