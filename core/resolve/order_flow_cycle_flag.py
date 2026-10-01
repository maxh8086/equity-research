"""Adapters to convert detected order flow cycles to storable models.

Converts CycleResult from the compute module into CompanyEvent records
to flag circular order relationships for company executives.

Pure: no I/O, no clock, no model involvement (R1).
"""

from datetime import datetime, date
from decimal import Decimal

from core.compute.order_flow_cycles import CycleResult
from core.db.models import CompanyEvent, CompanyEventSeverity, CompanyEventType


def _compute_severity(cycle_length: int | None) -> CompanyEventSeverity:
    """Severity is computed entirely by code, never by a model (R1).

    A 2-node cycle (A ↔ B) is HIGH: direct circular relationship.
    A 3-node cycle (A→B→C→A) is MEDIUM: potential coordination risk.
    3+ node cycles are LOW: less direct but still noteworthy for governance.
    """
    if cycle_length is None:
        # This shouldn't happen for a flagged cycle, but handle it defensively
        return CompanyEventSeverity.MEDIUM
    if cycle_length == 2:
        return CompanyEventSeverity.HIGH
    if cycle_length == 3:
        return CompanyEventSeverity.MEDIUM
    return CompanyEventSeverity.LOW


def to_company_event(
    source_isin: str,
    cycle_result: CycleResult,
    event_date: date,
    evidence_url: str,
    rule_version: str,
    as_of: datetime,
    extracted_by: str,
    content_hash: str,
    source_url: str,
) -> CompanyEvent | None:
    """Create a CompanyEvent from a CycleResult if it represents a detected cycle.

    Returns None if no cycle was detected (cycle_status == "no_cycle").
    Severity is computed by code from the cycle length.
    model_version is never set (R1).
    evidence_url is required (CLAUDE.md).
    """
    if cycle_result.cycle_status == "no_cycle":
        return None

    severity = _compute_severity(cycle_result.cycle_length)
    cycle_path_str = " → ".join(cycle_result.cycle_path) if cycle_result.cycle_path else "unknown"

    detail = (
        f"Circular order flow detected: {cycle_path_str} (length: {cycle_result.cycle_length}). "
        f"This may indicate coordinated procurement or governance concerns."
    )

    return CompanyEvent(
        isin=source_isin,
        event_type=CompanyEventType.ORDER_FLOW_CYCLE,
        severity=severity,
        event_date=event_date,
        metric="cycle_length",
        value=Decimal(cycle_result.cycle_length) if cycle_result.cycle_length else None,
        threshold=Decimal("2"),  # Flag any cycle of length 2 or more
        evidence_url=evidence_url,
        rule_version=rule_version,
        detail=detail,
        as_of=as_of,
        content_hash=content_hash,
        source_url=source_url,
        extracted_by=extracted_by,
        model_version=None,
    )
