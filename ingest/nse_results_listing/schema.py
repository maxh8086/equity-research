"""Strict model for one row of NSE's public financial-results listing.

The listing (`/api/corporates-financial-results?index=equities&symbol=<S>&period=<P>`)
is a JSON list, one object per filing. The adapter depends on three fields only,
and each must be present and well-typed or the row fails loudly. Every other
field NSE sends is ignored: it is never stored and never used, so a change in
one of them cannot alter what is loaded. The listing's `isin` is deliberately
not read (CLAUDE.md: it can hold old or non-equity ISINs); the ISIN comes from
the file, bhavcopy and the index lists.
"""

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BeforeValidator, ConfigDict

from core.timezones import IST
from ingest.schema import StrictModel

EXCHDISSTIME_FORMAT = "%d-%b-%Y %H:%M:%S"  # e.g. "17-Oct-2024 19:44:31", IST


def _parse_exchdisstime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"exchdisstime must be a string, got {type(value).__name__}")
    return datetime.strptime(value, EXCHDISSTIME_FORMAT).replace(tzinfo=IST)


DisseminatedAt = Annotated[AwareDatetime, BeforeValidator(_parse_exchdisstime)]


class ResultsListingRow(StrictModel):
    """`xbrl` is the attachment URL, or "-" / empty when the filing has none. `exchdisstime` is when NSE disseminated it."""

    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)

    symbol: str
    xbrl: str
    exchdisstime: DisseminatedAt

    def has_xbrl(self) -> bool:
        return self.xbrl.strip().lower().startswith(("http://", "https://"))
