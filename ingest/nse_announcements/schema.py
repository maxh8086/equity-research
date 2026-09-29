"""Strict Pydantic schema for NSE announcements API response."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, field_validator

from core.timezones import IST
from ingest.schema import StrictModel


class AnnouncementRow(StrictModel):
    """One row from NSE's /api/corporate-announcements?index=equities endpoint.

    `announced_on` is parsed from the string timestamp in IST.
    Never mutate this after parsing; extract all derived values (event dates, etc.) in classifier.
    """

    subject: str
    description: str | None = None
    announced_on: AwareDatetime
    isin: str
    symbol: str

    @field_validator("announced_on", mode="before")
    @classmethod
    def parse_announced_on(cls, v: str | datetime) -> datetime:
        """Parse announced_on string (NSE format: YYYY-MM-DD HH:MM:SS) to aware IST."""
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=IST)
            return v
        # Parse string in NSE format (assumed IST, no TZ in string)
        naive = datetime.fromisoformat(v)
        return naive.replace(tzinfo=IST)


class AnnouncementsListingResponse(StrictModel):
    """Response from NSE announcements API."""

    data: list[AnnouncementRow] = []
    pagination: dict | None = None
