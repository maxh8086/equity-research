"""Validation sample definitions (CLAUDE.md "Current phase").

A definition is never edited: a change is a new version beside it, and
tests/test_validate_samples.py fails if a recorded one changes.

The draw is `core.compute.sample.seeded_draw` over each index's membership as
of `membership_on`, read from the constituent lists known at that time
(`python -m validate sample` recomputes it from the database and compares).
Symbols are labels for people; the ISIN is the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.compute.sample import Swap, apply_swaps
from core.db.models import IndexCode
from core.timezones import IST


@dataclass(frozen=True)
class IndexDraw:
    index_code: IndexCode
    # SHA-256 of the constituent list the draw was taken from (the NSE archive
    # CSV, also kept as a test fixture).
    list_content_hash: str
    n: int
    drawn: tuple[tuple[str, str], ...]  # (isin, symbol) in draw order
    swaps: tuple[Swap, ...] = ()

    @property
    def drawn_isins(self) -> tuple[str, ...]:
        return tuple(isin for isin, _ in self.drawn)


@dataclass(frozen=True)
class SampleDefinition:
    version: str
    seed: str
    membership_on: datetime
    draws: tuple[IndexDraw, ...]
    # Human-curated company types the sample must cover, with the ISINs that
    # cover them. Labels for the coverage check only; nothing computes from them.
    coverage: tuple[tuple[str, tuple[str, ...]], ...]
    symbols: dict[str, str]  # isin -> symbol, for every drawn or swapped-in company

    def final(self, populations: dict[IndexCode, frozenset[str]]) -> dict[IndexCode, tuple[str, ...]]:
        return {d.index_code: apply_swaps(d.drawn_isins, populations[d.index_code], d.swaps) for d in self.draws}

    def isins(self) -> tuple[str, ...]:
        """Every ISIN in the final sample, after swaps, in draw order per index."""
        return tuple(
            isin for d in self.draws for isin in apply_swaps(d.drawn_isins, {s.in_isin for s in d.swaps}, d.swaps)
        )


SBILIFE = "INE123W01016"
KOTAKBANK = "INE237A01036"

SAMPLE_V1 = SampleDefinition(
    version="validation-sample/1",
    seed="validation-sample/1",
    membership_on=datetime(2026, 9, 19, 23, 59, 59, tzinfo=IST),
    draws=(
        IndexDraw(
            index_code=IndexCode.NIFTY_50,
            list_content_hash="9fb8832853c279448d2bc05f0e7dd5f460ed2ff35332fea8c40fc1250362ad28",
            n=10,
            drawn=(
                ("INE002A01018", "RELIANCE"),
                ("INE081A01020", "TATASTEEL"),
                ("INE397D01024", "BHARTIARTL"),
                ("INE059A01026", "CIPLA"),
                ("INE155A01022", "TMPV"),
                ("INE752E01010", "POWERGRID"),
                ("INE917I01010", "BAJAJ-AUTO"),
                ("INE040A01034", "HDFCBANK"),
                ("INE090A01021", "ICICIBANK"),
                (KOTAKBANK, "KOTAKBANK"),
            ),
            swaps=(
                Swap(
                    out_isin=KOTAKBANK,
                    in_isin=SBILIFE,
                    reason=(
                        "No insurer was drawn. The universe's only insurers are HDFCLIFE and SBILIFE, both in "
                        "Nifty 50; SBILIFE has the lower draw key. It replaces the last-drawn company of the "
                        "most over-represented type (KOTAKBANK, the third bank). No general insurer is in the "
                        "universe, so general insurance cannot be covered."
                    ),
                ),
            ),
        ),
        IndexDraw(
            index_code=IndexCode.NIFTY_NEXT_50,
            list_content_hash="e4654c80b911e3eaeb8044ec3a9e61d89b9db0702cc98b80e5a8fe86fbf83d5e",
            n=10,
            drawn=(
                ("INE242A01010", "IOC"),
                ("INE494B01023", "TVSMOTOR"),
                ("INE298A01020", "CUMMINSIND"),
                ("INE192R01011", "DMART"),
                ("INE414G01012", "MUTHOOTFIN"),
                ("INE931S01010", "ADANIENSOL"),
                ("INE118A01012", "BAJAJHLDNG"),
                ("INE318A01026", "PIDILITIND"),
                ("INE053F01010", "IRFC"),
                ("INE129A01019", "GAIL"),
            ),
        ),
    ),
    coverage=(
        ("bank", ("INE040A01034", "INE090A01021")),
        ("nbfc", ("INE414G01012", "INE053F01010")),
        ("insurer", (SBILIFE,)),
        ("capital_heavy_manufacturer", ("INE081A01020",)),
        ("group_with_many_subsidiaries", ("INE002A01018",)),
        ("restatement_or_demerger", ("INE155A01022",)),
    ),
    symbols={
        "INE002A01018": "RELIANCE", "INE081A01020": "TATASTEEL", "INE397D01024": "BHARTIARTL",
        "INE059A01026": "CIPLA", "INE155A01022": "TMPV", "INE752E01010": "POWERGRID",
        "INE917I01010": "BAJAJ-AUTO", "INE040A01034": "HDFCBANK", "INE090A01021": "ICICIBANK",
        KOTAKBANK: "KOTAKBANK", SBILIFE: "SBILIFE", "INE242A01010": "IOC", "INE494B01023": "TVSMOTOR",
        "INE298A01020": "CUMMINSIND", "INE192R01011": "DMART", "INE414G01012": "MUTHOOTFIN",
        "INE931S01010": "ADANIENSOL", "INE118A01012": "BAJAJHLDNG", "INE318A01026": "PIDILITIND",
        "INE053F01010": "IRFC", "INE129A01019": "GAIL",
    },  # fmt: skip
)

SAMPLES = {SAMPLE_V1.version: SAMPLE_V1}
CURRENT = SAMPLE_V1
