"""Strict Pydantic schema for NSE shareholding listing API response."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, ConfigDict, Field, field_validator

from core.timezones import IST
from ingest.schema import StrictModel


class ShareholdingListingRow(StrictModel):
    """One row from NSE's /api/corporate-share-holdings-master?index=equities endpoint.

    `broadcast_date` is parsed from the string timestamp in IST.
    `isin` field is never used (CLAUDE.md says it can hold old or non-equity ISINs);
    the ISIN comes from the XBRL file, bhavcopy, and index lists instead.
    Never mutate this after parsing; extract all derived values in the adapter.
    """

    model_config = ConfigDict(populate_by_name=True)  # Accept both snake_case and camelCase

    symbol: str
    isin: str  # Not used; kept for schema validation only
    broadcast_date: AwareDatetime = Field(alias="broadcastDate")
    xbrl_file_url: str = Field(alias="xbrlFileUrl")
    revision_date: AwareDatetime | None = Field(default=None, alias="revisionDate")
    revision_remark: str | None = Field(default=None, alias="revisionRemark")

    @field_validator("broadcast_date", mode="before")
    @classmethod
    def parse_broadcast_date(cls, v: str | datetime) -> datetime:
        """Parse broadcast_date string (NSE format: YYYY-MM-DD HH:MM:SS) to aware IST."""
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=IST)
            return v
        # Parse string in NSE format (assumed IST, no TZ in string)
        naive = datetime.fromisoformat(v)
        return naive.replace(tzinfo=IST)

    @field_validator("revision_date", mode="before")
    @classmethod
    def parse_revision_date(cls, v: str | datetime | None) -> datetime | None:
        """Parse revision_date string to aware IST."""
        if v is None:
            return None
        if isinstance(v, datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=IST)
            return v
        # Parse string in NSE format (assumed IST, no TZ in string)
        naive = datetime.fromisoformat(v)
        return naive.replace(tzinfo=IST)


class ShareholdingListingResponse(StrictModel):
    """Response from NSE shareholding listing API."""

    data: list[ShareholdingListingRow] = []
