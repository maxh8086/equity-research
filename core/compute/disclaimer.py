"""The text every baseline, stop or action carries, and the check that it did.

CLAUDE.md fixes the wording. It lives here, as a pure function of the as-of
date, so that a renderer cannot paraphrase it and a test can assert on the
exact bytes. `omissions` is the assertion helper: a rendered report that does
not carry the disclaimer for its own as-of date is a bug, not a style choice.
"""

from __future__ import annotations

from datetime import date

RULE_VERSION = "disclaimer/1"

TEMPLATE = (
    "**Assumed baseline.** Computed by code from the stated assumptions and\n"
    "data known as of {as_of}. This is not investment advice or an\n"
    "instruction to trade. Any purchase, sale or position change requires\n"
    "human review of current facts, taxes and personal circumstances."
)


def disclaimer(as_of: date) -> str:
    """The disclaimer for a report whose knowledge cut-off is `as_of`."""
    return TEMPLATE.format(as_of=as_of.isoformat())


def carries_disclaimer(rendered: str, as_of: date) -> bool:
    """Whether `rendered` carries the disclaimer for its own as-of date.

    Line breaks are normalised because a renderer may rewrap; nothing else is.
    """
    return _normalise(disclaimer(as_of)) in _normalise(rendered)


def _normalise(text: str) -> str:
    return " ".join(text.split())
