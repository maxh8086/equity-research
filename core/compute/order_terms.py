"""Decide, by code, whether a filing's text announces an order win.

Store ⑦ (`order_win` / `order_status`) tracks declared versus executed orders.
Getting anything into it starts with a text decision, and that decision is
made here: R1 forbids asking a model whether a threshold was crossed, and
"does this announcement describe an order win" is exactly that. A model may
later summarise the narrative; it never classifies and never supplies the
number.

The hard part in Indian exchange filings is not finding the word *order*. It
is that the word is dominated by **tax and legal orders** — GST demands,
assessment orders, tribunal rulings — filed under the same Regulation 30 as a
genuine contract award. So three lists, all hardcoded research decisions under
a `rule_version` (CLAUDE.md: research decisions belong in code, not config):

- `AWARD_PHRASES` — an order was won
- `PRE_AWARD_PHRASES` — lowest bidder, not yet awarded; a weaker, separate
  class, because L1 is a declaration and not an order
- `EXCLUSION_PHRASES` — a tax, regulatory or judicial order

One trap dictates the design. Nearly every NSE/BSE announcement cites
*SEBI (Listing Obligations and Disclosure Requirements) Regulations, 2015*,
so excluding on `sebi` or `regulation` throws away the entire corpus. The
boilerplate is therefore redacted before exclusions are matched, and the
exclusion list holds specific multi-word phrases rather than bare words.

Text that matches both an award phrase and an exclusion is `AMBIGUOUS`, which
means quarantine: a filing can mention a tax order and a contract in the same
breath, and guessing which one it is about is how look-alike rows get into a
store that is append-only.

Letting the exclusion win instead would be quieter and wrong — a real award
would vanish because a tax order shared the page, and CLAUDE.md is explicit
that a gap must be visible rather than silently favourable. The volume worry
that would motivate it is handled a stage earlier: `search_query` keeps tax
wording out of the candidate set, so most tax orders never reach `classify`
at all. The query cuts volume; the classifier makes the careful call.

Pure: callers pass the text in. Source-agnostic by design — the same rules
apply to an NSE announcement, a BSE announcement, a Screener full-text search
result or a file dropped in by hand.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

RULE_VERSION = "order_terms/1"

# An order was won. Ordered longest-first at match time so that the more
# specific phrase is the one reported.
AWARD_PHRASES: tuple[str, ...] = (
    "order received",
    "receipt of order",
    "received an order",
    "award of order",
    "awarding of order",
    "award of contract",
    "contract award",
    "notification of award",
    "letter of award",
    "letter of intent",
    "letter of acceptance",
    "large order",
    "order for procurement",
    "bagged an order",
    "bagged order",
    "repeat order",
    "additional order",
    "work order",
    "supply order",
    "purchase order",
    "wins order",
    "won an order",
    "secures order",
    "secured an order",
    "order win",
    "order intake",
    "order inflow",
    "export order",
    "domestic order",
    "new order",
)

# Declared, not awarded. CLAUDE.md ⑦ is declared versus executed; L1 sits one
# step earlier still, so it never counts as a win on its own.
PRE_AWARD_PHRASES: tuple[str, ...] = (
    "l1 bidder",
    "lowest bidder",
    "lowest evaluated bidder",
    "emerged as l1",
    "emerged as the lowest",
    "declared l1",
    "preferred bidder",
    "selected as the preferred",
)

# A tax, regulatory or judicial order. Multi-word on purpose: bare "tax",
# "order" or "sebi" would match the boilerplate every filing carries.
EXCLUSION_PHRASES: tuple[str, ...] = (
    "commissioner",
    "income tax",
    "goods and services tax",
    "assessment order",
    "demand order",
    "penalty order",
    "rectification order",
    "refund order",
    "adjudication order",
    "adjudicating authority",
    "assessing officer",
    "show cause",
    "show-cause",
    "appellate",
    "cestat",
    "itat",
    "nclt",
    "nclat",
    "tribunal",
    "court order",
    "stay order",
    "provisional attachment",
    "garnishee",
    "faceless assessment",
    "input tax credit",
    "tax deducted at source",
)

# Order-like wording in a context that is not a customer order at all: an
# acquisition LoI, a related-party purchase order. These are not tax orders,
# so they sit apart from EXCLUSION_PHRASES, but they disqualify a match the
# same way.
NON_ORDER_CONTEXT_PHRASES: tuple[str, ...] = (
    "related party",
    "scheme of arrangement",
    "scheme of amalgamation",
    "acquisition of equity",
    "acquisition of shares",
    "share purchase agreement",
    "share subscription",
    "equity stake",
    "stake in",
    "joint venture agreement",
)

# Redacted before exclusions are matched. Every Regulation 30 filing carries
# this, so matching on it would exclude the whole corpus.
BOILERPLATE_PHRASES: tuple[str, ...] = (
    "sebi (listing obligations and disclosure requirements) regulations",
    "listing obligations and disclosure requirements",
    "securities and exchange board of india",
    "regulation 30",
    "regulation 51",
    "lodr",
)

# Indian money units, as exact multipliers. Decimal, never float.
UNIT_MULTIPLIERS: dict[str, Decimal] = {
    "crore": Decimal("10000000"),
    "crores": Decimal("10000000"),
    "cr": Decimal("10000000"),
    "lakh": Decimal("100000"),
    "lakhs": Decimal("100000"),
    "lac": Decimal("100000"),
    "lacs": Decimal("100000"),
    "million": Decimal("1000000"),
    "mn": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "bn": Decimal("1000000000"),
}

_CURRENCY = r"(?:rs\.?|inr|₹|rupees)"
_NUMBER = r"\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT = "|".join(sorted(UNIT_MULTIPLIERS, key=len, reverse=True))
_AMOUNT_RE = re.compile(
    rf"{_CURRENCY}\s*({_NUMBER})\s*({_UNIT})\b|({_NUMBER})\s*({_UNIT})\s*{_CURRENCY}",
    re.IGNORECASE,
)


class Verdict(StrEnum):
    AWARD = "award"
    PRE_AWARD = "pre_award"
    EXCLUDED = "excluded"  # a tax, regulatory or judicial order
    AMBIGUOUS = "ambiguous"  # both kinds present: quarantine, never guess
    NO_MATCH = "no_match"


@dataclass(frozen=True)
class Match:
    phrase: str
    start: int
    end: int
    quote: str  # the verbatim window, kept so a person can check the call


@dataclass(frozen=True)
class Amount:
    """A money figure parsed by code from the text, never supplied by a model."""

    quote: str
    inr: Decimal
    unit: str
    start: int
    end: int


@dataclass(frozen=True)
class Classification:
    verdict: Verdict
    awards: tuple[Match, ...]
    pre_awards: tuple[Match, ...]
    exclusions: tuple[Match, ...]
    amounts: tuple[Amount, ...]
    rule_version: str = RULE_VERSION

    @property
    def is_lead(self) -> bool:
        """Whether this is worth confirming against an exchange filing.

        CLAUDE.md: a reported order win is a lead, counted only once confirmed.
        """
        return self.verdict in (Verdict.AWARD, Verdict.PRE_AWARD)


def classify(text: str, quote_window: int = 60) -> Classification:
    """Classify `text` deterministically. No model, no network, no clock."""
    lowered = text.lower()
    redacted = _redact(lowered, BOILERPLATE_PHRASES)

    awards = _find(lowered, AWARD_PHRASES, text, quote_window)
    pre_awards = _find(lowered, PRE_AWARD_PHRASES, text, quote_window)
    exclusions = _find(
        redacted, EXCLUSION_PHRASES + NON_ORDER_CONTEXT_PHRASES, text, quote_window
    )
    amounts = parse_amounts(text)

    if awards and exclusions:
        verdict = Verdict.AMBIGUOUS
    elif exclusions and not pre_awards:
        verdict = Verdict.EXCLUDED
    elif awards:
        verdict = Verdict.AWARD
    elif pre_awards and not exclusions:
        verdict = Verdict.PRE_AWARD
    elif pre_awards:
        verdict = Verdict.AMBIGUOUS
    else:
        verdict = Verdict.NO_MATCH

    return Classification(verdict, awards, pre_awards, exclusions, amounts)


def parse_amounts(text: str) -> tuple[Amount, ...]:
    """Every INR figure in `text`, normalised to rupees as `Decimal`.

    The quote is kept beside the number so a reader can check the parse against
    the source, the way a brokerage target price is checked (CLAUDE.md, R1).
    """
    out: list[Amount] = []
    for m in _AMOUNT_RE.finditer(text):
        number, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        multiplier = UNIT_MULTIPLIERS[unit.lower()]
        out.append(
            Amount(
                quote=m.group(0),
                inr=Decimal(number.replace(",", "")) * multiplier,
                unit=unit.lower(),
                start=m.start(),
                end=m.end(),
            )
        )
    return tuple(out)


def _redact(lowered: str, phrases: Iterable[str]) -> str:
    """Blank out spans, keeping length so offsets stay comparable."""
    out = lowered
    for phrase in phrases:
        start = 0
        while (i := out.find(phrase, start)) != -1:
            out = out[:i] + " " * len(phrase) + out[i + len(phrase) :]
            start = i + len(phrase)
    return out


def _find(haystack: str, phrases: Iterable[str], original: str, window: int) -> tuple[Match, ...]:
    """Every phrase occurrence, longest phrase first, no overlapping reports."""
    found: list[Match] = []
    taken: list[tuple[int, int]] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        for m in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            lo, hi = max(0, m.start() - window), min(len(original), m.end() + window)
            found.append(Match(phrase, m.start(), m.end(), original[lo:hi].strip()))
    return tuple(sorted(found, key=lambda x: x.start))


def search_query(include_pre_award: bool = False) -> str:
    """A full-text search string built from the same lists the classifier uses.

    One source of truth: a phrase added above changes both the search that
    finds candidates and the code that judges them, so recall and precision
    cannot drift apart.

    The whole disjunction is parenthesised and the negatives sit outside it.
    Without those parentheses a search engine binds `or` more loosely than the
    implicit `and`, so the negatives filter only the first clause and every
    other branch comes back unfiltered — which, on this corpus, means tax
    orders.

    Boilerplate terms are never negated here: excluding `sebi` or
    `regulation 30` would exclude the entire corpus. Precision against tax
    orders comes from `classify`, which redacts that boilerplate first; the
    query is only there to cut the candidate set down.

    The syntax is the common `"phrase" or "phrase"` with `-"term"` negation.
    Confirm it against the target engine before relying on it: engines differ
    on precedence and on whether `or` must be uppercase.
    """
    phrases = AWARD_PHRASES + (PRE_AWARD_PHRASES if include_pre_award else ())
    positives = " or ".join(f'"{p}"' for p in phrases)
    negatives = " ".join(f'-"{p}"' for p in EXCLUSION_PHRASES)
    return f"{negatives} ({positives})"
