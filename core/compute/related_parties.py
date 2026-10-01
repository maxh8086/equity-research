"""Curated related-party names for the order-book concentration flag.

A counterparty is a related party of a company only when a human put the name
in `RELATED_PARTIES` with a dated validity and a public source. Matching is
exact on the normalised name, or an exact curated alias: no fuzzy, substring or
prefix guessing, and no model (R1). A lookalike name ("Alpha Infra Finance" for
"Alpha Infra") is a different legal entity and does not match.

The table is seeded empty: no relationship is entered without a public source
that can be cited in its `source_note`, and none is invented. Add entries with
the filing or annual-report reference (for example the Ind AS 24 related-party
note of the company's annual report, or a Regulation 23 disclosure) and the
dates the relationship held.

Pure: no I/O, no clock.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

RULE_VERSION = "related_parties/1"

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
# Legal-form suffixes, stripped from the end of a name only.
_SUFFIXES = frozenset({"ltd", "limited", "pvt", "private"})


def normalise_name(name: str) -> str:
    """Casefold, drop punctuation, strip trailing Ltd/Limited/Pvt/Private, collapse spaces.

    "&" becomes a separator like any other punctuation and is not expanded.
    """
    tokens = _NON_ALNUM.sub(" ", name.casefold()).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass(frozen=True)
class RelatedPartyEntry:
    """`names` are counterparties that are related parties of the company `isin`.

    Valid from `valid_from` to `valid_to` inclusive (None = still valid).
    `source_note` cites the public source; it is required.
    """

    isin: str
    names: tuple[str, ...]
    valid_from: date
    valid_to: date | None
    source_note: str

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        if not self.source_note.strip():
            raise ValueError("a related-party entry needs a source note")

    def valid_on(self, d: date) -> bool:
        return self.valid_from <= d and (self.valid_to is None or d <= self.valid_to)


# Curated by hand. Empty until a relationship can be cited to a public source.
RELATED_PARTIES: tuple[RelatedPartyEntry, ...] = ()


def match_related(
    counterparty: str | None,
    order_date: date,
    isin: str,
    entries: Sequence[RelatedPartyEntry],
) -> RelatedPartyEntry | None:
    """The entry naming `counterparty` as a related party of `isin` on `order_date`, else None."""
    if counterparty is None:
        return None
    wanted = normalise_name(counterparty)
    if not wanted:
        return None
    for entry in entries:
        if entry.isin != isin or not entry.valid_on(order_date):
            continue
        if any(normalise_name(n) == wanted for n in entry.names):
            return entry
    return None
