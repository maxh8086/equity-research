"""Guidance claims: everything about them that code decides, not a model (R1).

A model reads a transcript and hands back verbatim strings -- the quote, the
hedging phrase inside it, the number as printed, the period label. Nothing
graded. This module turns those strings into the graded fields the store
keeps: hedge strength, specificity, the parsed value, and whether guidance was
raised, lowered, maintained or reshaped against the prior quarter.

Why the split matters. "We will reach 15% margin" and "we are working towards
15% margin" are the same sentence to a number extractor and opposite claims to
a credibility ledger. If a model assigns the strength, the ledger grades
management on a judgement that can drift silently between model versions. Here
the mapping is a table under RULE_VERSION: it changes only in a commit, and
every claim records which version graded it.

A phrase the lexicon does not know is never guessed. It comes back as None so
the caller can quarantine the claim, and reviewing the quarantine is how the
lexicon grows -- the same shape as the news alias table in CLAUDE.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

# Bump when the lexicon, the parser or the comparison changes. Stored on every
# row, so a reparse under a new rule adds rows rather than revising them.
RULE_VERSION = "guidance-2026-09-29"


class HedgeStrength(StrEnum):
    """CLAUDE.md store 2: will > expect > aim to > working towards."""

    WILL = "will"
    EXPECT = "expect"
    AIM_TO = "aim_to"
    WORKING_TOWARDS = "working_towards"


# Ordering is explicit rather than declaration order: a future member inserted
# in the middle must be ranked deliberately, not by where it was typed.
HEDGE_RANK: dict[HedgeStrength, int] = {
    HedgeStrength.WILL: 4,
    HedgeStrength.EXPECT: 3,
    HedgeStrength.AIM_TO: 2,
    HedgeStrength.WORKING_TOWARDS: 1,
}


class ClaimSection(StrEnum):
    """Where in the source the claim was made.

    Prepared remarks are scripted and approved; a Q&A answer is not. The
    distinction is kept because the delivery rates may differ, not because
    either is discounted up front.
    """

    PREPARED_REMARKS = "prepared_remarks"
    QA = "qa"
    MEDIA_INTERVIEW = "media_interview"


class Specificity(StrEnum):
    """How falsifiable the claim is, from what the words actually contain."""

    POINT = "point"  # one number: "15% margin"
    RANGE = "range"  # two: "15-17%"
    BOUND = "bound"  # one-sided: "at least 15%"
    DIRECTIONAL = "directional"  # no number: "margins should improve"


class Unit(StrEnum):
    PERCENT = "percent"
    BPS = "bps"
    INR_CRORE = "inr_crore"
    MULTIPLE = "multiple"
    COUNT = "count"


class Direction(StrEnum):
    """How a claim moved against the same company's prior claim for the same
    metric and period. Computed here; never extracted."""

    NEW = "new"
    RAISED = "raised"
    LOWERED = "lowered"
    MAINTAINED = "maintained"
    RESHAPED = "reshaped"  # the band moved both ways, e.g. 15-17% -> 14-18%
    NOT_COMPARABLE = "not_comparable"  # different unit, period or specificity


class HedgeChange(StrEnum):
    """The wording moved even where the number did not."""

    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    UNCHANGED = "unchanged"


# --------------------------------------------------------------------------- #
# Hedge lexicon
# --------------------------------------------------------------------------- #

# Leading words that carry no commitment and only get in the way of a lookup.
# Repeated, because a speaker stacks them ("so we would expect"), and every
# alternative is \b-anchored: without that, the bare "i" ate the first letter
# of "intend" and made that lexicon key unreachable.
_LEAD = re.compile(
    r"^(?:(?:so|and|but|i|we|they|the\s+company|our|management|"
    r"would|do|does|are|is|am|have|has)\b\s*)+"
)
_TRAIL = re.compile(r"\s*\b(?:that|to|for|at|in|on|towards?|of)\s*$")
_SPACE = re.compile(r"\s+")

# The research decision, in code (CLAUDE.md: "Hardcoded by design, not config").
# Keys are the normalised phrase; a phrase absent here is unmapped, not weak.
HEDGE_LEXICON: dict[str, HedgeStrength] = {
    # WILL -- a commitment, stated without a qualifier
    "will": HedgeStrength.WILL,
    "will be": HedgeStrength.WILL,
    "will definitely": HedgeStrength.WILL,
    "shall": HedgeStrength.WILL,
    "committed": HedgeStrength.WILL,
    "guiding": HedgeStrength.WILL,
    "guidance is": HedgeStrength.WILL,
    "confident": HedgeStrength.WILL,
    # EXPECT -- a forecast the speaker owns but does not promise
    "expect": HedgeStrength.EXPECT,
    "expects": HedgeStrength.EXPECT,
    "expecting": HedgeStrength.EXPECT,
    "anticipate": HedgeStrength.EXPECT,
    "foresee": HedgeStrength.EXPECT,
    "project": HedgeStrength.EXPECT,
    "believe": HedgeStrength.EXPECT,
    "believes": HedgeStrength.EXPECT,
    "see": HedgeStrength.EXPECT,
    "should": HedgeStrength.EXPECT,
    "likely": HedgeStrength.EXPECT,
    # AIM_TO -- an intention, with the outcome not claimed
    "aim": HedgeStrength.AIM_TO,
    "aiming": HedgeStrength.AIM_TO,
    "target": HedgeStrength.AIM_TO,
    "targeting": HedgeStrength.AIM_TO,
    "intend": HedgeStrength.AIM_TO,
    "plan": HedgeStrength.AIM_TO,
    "planning": HedgeStrength.AIM_TO,
    "looking": HedgeStrength.AIM_TO,
    # WORKING_TOWARDS -- effort, with no claim about arriving
    "working": HedgeStrength.WORKING_TOWARDS,
    "work": HedgeStrength.WORKING_TOWARDS,
    "striving": HedgeStrength.WORKING_TOWARDS,
    "endeavouring": HedgeStrength.WORKING_TOWARDS,
    "endeavoring": HedgeStrength.WORKING_TOWARDS,
    "trying": HedgeStrength.WORKING_TOWARDS,
    "hoping": HedgeStrength.WORKING_TOWARDS,
    "hope": HedgeStrength.WORKING_TOWARDS,
    "like": HedgeStrength.WORKING_TOWARDS,  # "we would like to": normalise drops the "would"
}


def normalise_hedge(phrase: str) -> str:
    """Lowercase, collapse space, and drop the pronouns and prepositions around the verb.

    "We are working towards" and "the company is working towards" both reduce
    to "working", so the lexicon stays a list of verbs rather than a list of
    sentences.
    """
    text = _SPACE.sub(" ", phrase.strip().lower())
    text = _LEAD.sub("", text, count=1)
    text = _TRAIL.sub("", text, count=1)
    return text.strip()


def hedge_strength(phrase: str) -> HedgeStrength | None:
    """The strength of a verbatim hedging phrase, or None if the lexicon has no entry.

    None is a quarantine, never a default. Defaulting an unknown phrase to the
    weakest bucket would quietly forgive missed promises; defaulting it to the
    strongest would invent commitments.
    """
    return HEDGE_LEXICON.get(normalise_hedge(phrase))


def compare_hedges(previous: HedgeStrength, current: HedgeStrength) -> HedgeChange:
    if HEDGE_RANK[current] > HEDGE_RANK[previous]:
        return HedgeChange.STRENGTHENED
    if HEDGE_RANK[current] < HEDGE_RANK[previous]:
        return HedgeChange.WEAKENED
    return HedgeChange.UNCHANGED


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LOWER_BOUND = re.compile(
    r"\b(?:at\s+least|no\s+less\s+than|north\s+of|upwards?\s+of|more\s+than|"
    r"greater\s+than|over|above|minimum\s+of|plus)\b"
)
_UPPER_BOUND = re.compile(
    r"\b(?:at\s+most|no\s+more\s+than|less\s+than|under|below|up\s+to|maximum\s+of|within)\b"
)
_UNITS: tuple[tuple[re.Pattern[str], Unit], ...] = (
    (re.compile(r"\bbps\b|\bbasis\s+points?\b"), Unit.BPS),
    (re.compile(r"%|\bper\s*cent\b|\bpercent(?:age)?\b"), Unit.PERCENT),
    (re.compile(r"\bcrores?\b|\bcr\b|₹|\brs\.?\b|\binr\b"), Unit.INR_CRORE),
    (re.compile(r"\d\s*x\b|\btimes\b"), Unit.MULTIPLE),
)


class ValueUnparsed(ValueError):
    """The printed number could not be read. The claim is quarantined, not dropped."""


@dataclass(frozen=True)
class ParsedValue:
    low: Decimal | None
    high: Decimal | None
    unit: Unit
    specificity: Specificity


def _decimal(token: str) -> Decimal:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - the regex admits only digits
        raise ValueUnparsed(f"{token!r} is not a number") from exc


def parse_value(value_text: str | None, *, unit_hint: str = "") -> ParsedValue:
    """Read the number a model copied out of the quote. Never computes anything.

    `value_text` is the printed phrase, e.g. "15-17%", "at least Rs 2,000
    crore", "1.5x". None means the claim carried no number and is DIRECTIONAL.
    `unit_hint` is the surrounding quote, consulted only for the unit when the
    phrase itself does not name one.

    No unit conversion happens here. A figure in lakh is read as printed and
    tagged COUNT rather than silently multiplied into crore: rescaling a
    management number is exactly the arithmetic R1 keeps away from guesswork,
    and the conversion belongs with the resolution code that knows what the
    matching XBRL fact is denominated in.
    """
    if value_text is None or not value_text.strip():
        return ParsedValue(None, None, Unit.COUNT, Specificity.DIRECTIONAL)

    text = _SPACE.sub(" ", value_text.strip().lower())
    numbers = [_decimal(m.group()) for m in _NUMBER.finditer(text)]
    if not numbers:
        raise ValueUnparsed(f"no number in {value_text!r}")
    if len(numbers) > 2:
        raise ValueUnparsed(f"{len(numbers)} numbers in {value_text!r}; a claim carries one or two")

    haystack = f"{text} {_SPACE.sub(' ', unit_hint.strip().lower())}"
    unit = next((u for pattern, u in _UNITS if pattern.search(haystack)), Unit.COUNT)

    if len(numbers) == 2:
        low, high = sorted(numbers)
        return ParsedValue(low, high, unit, Specificity.RANGE)
    (only,) = numbers
    if _LOWER_BOUND.search(text):
        return ParsedValue(only, None, unit, Specificity.BOUND)
    if _UPPER_BOUND.search(text):
        return ParsedValue(None, only, unit, Specificity.BOUND)
    return ParsedValue(only, only, unit, Specificity.POINT)


# --------------------------------------------------------------------------- #
# Direction against the prior claim
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComparableClaim:
    """The graded part of a claim, as the comparison needs it."""

    metric: str
    period_label: str
    value: ParsedValue
    hedge: HedgeStrength


def compare_claims(previous: ComparableClaim | None, current: ComparableClaim) -> Direction:
    """Whether `current` raised, lowered, maintained or reshaped `previous`.

    Both sides must be the same metric, the same period and the same unit --
    "16% margin for FY27" says nothing about "16% margin for FY28", and a
    number in bps is not a number in percent. Anything else is
    NOT_COMPARABLE, which is a finding for review rather than a comparison
    made on a guess.

    A one-sided bound is compared on the side it states: a floor raised from
    15% to 16% is RAISED even though neither claim has a ceiling.

    WITHDRAWN is deliberately not produced here. It is the absence of a claim,
    which only a later transcript covering the same metric can establish, and
    CLAUDE.md gives that its own status (SILENT) resolved against filings.
    """
    if previous is None:
        return Direction.NEW
    if previous.metric != current.metric or previous.period_label != current.period_label:
        return Direction.NOT_COMPARABLE
    if previous.value.unit != current.value.unit:
        return Direction.NOT_COMPARABLE
    if Specificity.DIRECTIONAL in (previous.value.specificity, current.value.specificity):
        return Direction.NOT_COMPARABLE

    pairs = [
        (p, c)
        for p, c in (
            (previous.value.low, current.value.low),
            (previous.value.high, current.value.high),
        )
        if p is not None and c is not None
    ]
    if not pairs:
        # One claim states only a floor, the other only a ceiling: nothing lines up.
        return Direction.NOT_COMPARABLE

    up = any(c > p for p, c in pairs)
    down = any(c < p for p, c in pairs)
    if up and down:
        return Direction.RESHAPED
    if up:
        return Direction.RAISED
    if down:
        return Direction.LOWERED
    return Direction.MAINTAINED
