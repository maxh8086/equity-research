"""Seeded sample draws with recorded swaps. Pure functions, no I/O.

CLAUDE.md "Current phase": the validation sample is drawn with a fixed seed
from membership as of a fixed date; companies may be swapped in to cover
required types, and each swap is recorded with its reason.

The draw ranks each ISIN by SHA-256 of "<seed>:<isin>" and takes the lowest
`n`. Unlike `random.Random(seed)`, the result depends only on the seed and the
set of ISINs: not on their order, the Python version, or the library.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Swap:
    out_isin: str
    in_isin: str
    reason: str


class InvalidSwap(ValueError):
    pass


def draw_key(seed: str, isin: str) -> str:
    return hashlib.sha256(f"{seed}:{isin}".encode()).hexdigest()


def seeded_draw(population: Collection[str], *, seed: str, n: int) -> tuple[str, ...]:
    """The `n` members of `population` with the lowest draw keys, in draw order."""
    members = set(population)
    if len(members) != len(population):
        raise ValueError("population has duplicates")
    if not 0 < n <= len(members):
        raise ValueError(f"cannot draw {n} from {len(members)}")
    return tuple(sorted(members, key=lambda isin: draw_key(seed, isin))[:n])


def apply_swaps(drawn: Sequence[str], population: Collection[str], swaps: Sequence[Swap]) -> tuple[str, ...]:
    """Replace each swap's `out_isin` with its `in_isin`, in place and in order.

    A swap must take out a company currently in the sample and bring in one
    from the same population that is not in it; anything else is an error,
    never silently skipped.
    """
    sample = list(drawn)
    members = set(population)
    for s in swaps:
        if not s.reason.strip():
            raise InvalidSwap(f"swap {s.out_isin} -> {s.in_isin} has no reason")
        if s.out_isin not in sample:
            raise InvalidSwap(f"{s.out_isin} is not in the sample")
        if s.in_isin not in members:
            raise InvalidSwap(f"{s.in_isin} is not in the population")
        if s.in_isin in sample:
            raise InvalidSwap(f"{s.in_isin} is already in the sample")
        sample[sample.index(s.out_isin)] = s.in_isin
    return tuple(sample)
