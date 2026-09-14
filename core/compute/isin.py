"""ISIN validation (ISO 6166). ISIN is the entity key, so malformed ones never enter a store."""

import re

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_BODY_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}$")


def isin_check_digit(body: str) -> int:
    """Check digit for the first 11 characters of an ISIN.

    Letters expand to two digits (A=10 .. Z=35), then Luhn is applied to the
    expanded digit string with the check digit position as the rightmost.
    """
    if not _BODY_RE.match(body):
        raise ValueError(f"ISIN body must be 2 letters + 9 alphanumerics, got {body!r}")
    digits = "".join(str(int(ch, 36)) for ch in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def is_valid_isin(value: str) -> bool:
    if not _ISIN_RE.match(value):
        return False
    return isin_check_digit(value[:11]) == int(value[11])
