"""CWIP-to-gross-block step-function detector.

Pure functions, no I/O. Detects capacity commissioning: when gross block
rises >= 20% QoQ while CWIP (Capital Work-In-Progress) falls, the company
has converted construction work into operating capacity.

Rule version: cwip_step_v1
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class CwipEvent:
    """A detected CWIP-to-gross-block commissioning event."""

    isin: str
    event_date: date  # period_end of the triggering quarter
    gross_block_prior: Decimal
    gross_block_current: Decimal
    cwip_prior: Decimal
    cwip_current: Decimal
    gross_block_qoq_pct: Decimal  # (current - prior) / prior
    cwip_delta: Decimal  # current - prior (negative = fell)
    rule_version: str


def detect_cwip_commissioning(
    isin: str,
    periods: list[tuple[date, Decimal, Decimal]],
    threshold_pct: Decimal = Decimal("0.20"),
) -> list[CwipEvent]:
    """Detect quarters where gross block rose >= threshold while CWIP fell.

    Args:
        isin: The ISIN of the company.
        periods: List of (period_end, gross_block, cwip) tuples, sorted
            chronologically (oldest first). Values must be Decimal.
        threshold_pct: Minimum QoQ gross block increase to flag (default 0.20
            = 20%).

    Returns:
        List of CwipEvent, one per triggering consecutive quarter pair.
        Returns empty list if fewer than 2 periods are provided.

    Rules (per CLAUDE.md):
        - gross block increased by >= threshold_pct QoQ
        - CWIP decreased strictly (cwip_current < cwip_prior)

    All arithmetic uses Decimal, never float (CLAUDE.md).
    """
    if len(periods) < 2:
        return []

    events: list[CwipEvent] = []
    
    RULE_VERSION = "cwip_step_v1"

    for prior, current in zip(periods, periods[1:]):
        prior_date, prior_gb, prior_cwip = prior
        current_date, current_gb, current_cwip = current

        if prior_gb == Decimal("0"):
            # Cannot compute a meaningful ratio; skip
            continue

        gb_qoq_pct = (current_gb - prior_gb) / prior_gb
        cwip_delta = current_cwip - prior_cwip

        if gb_qoq_pct >= threshold_pct and cwip_delta < Decimal("0"):
            events.append(
                CwipEvent(
                    isin=isin,
                    event_date=current_date,
                    gross_block_prior=prior_gb,
                    gross_block_current=current_gb,
                    cwip_prior=prior_cwip,
                    cwip_current=current_cwip,
                    gross_block_qoq_pct=gb_qoq_pct,
                    cwip_delta=cwip_delta,
                    rule_version=RULE_VERSION,
                )
            )

    return events
