"""Adapters to convert CwipEvent to storable models."""

from datetime import datetime
from decimal import Decimal

from core.compute.cwip_detector import CwipEvent
from core.db.models import CompanyEvent, CompanyEventSeverity, CompanyEventType


def _compute_severity(gross_block_qoq_pct: Decimal) -> CompanyEventSeverity:
    """Severity is computed entirely by code, never by a model (R1).

    >= 50%: CRITICAL
    >= 30%: HIGH
    >= 20%: MEDIUM (the minimum threshold)
    """
    if gross_block_qoq_pct >= Decimal("0.50"):
        return CompanyEventSeverity.CRITICAL
    if gross_block_qoq_pct >= Decimal("0.30"):
        return CompanyEventSeverity.HIGH
    return CompanyEventSeverity.MEDIUM


def to_company_event(
    cwip_event: CwipEvent,
    evidence_url: str,
    as_of: datetime,
    extracted_by: str,
    content_hash: str,
    source_url: str,
) -> CompanyEvent:
    """Create a CompanyEvent from a CwipEvent.

    Severity is computed by code from the gross block QoQ percentage.
    model_version is never set (R1).
    evidence_url is required (CLAUDE.md).
    """
    severity = _compute_severity(cwip_event.gross_block_qoq_pct)

    return CompanyEvent(
        isin=cwip_event.isin,
        event_type=CompanyEventType.CWIP_TO_GROSS_BLOCK,
        severity=severity,
        event_date=cwip_event.event_date,
        metric="gross_block_qoq_pct",
        value=cwip_event.gross_block_qoq_pct,
        threshold=Decimal("0.20"),
        evidence_url=evidence_url,
        rule_version=cwip_event.rule_version,
        detail=(
            f"Gross block rose {cwip_event.gross_block_qoq_pct:.4%} QoQ "
            f"({cwip_event.gross_block_prior} -> {cwip_event.gross_block_current}); "
            f"CWIP fell {cwip_event.cwip_delta} "
            f"({cwip_event.cwip_prior} -> {cwip_event.cwip_current})"
        ),
        as_of=as_of,
        content_hash=content_hash,
        source_url=source_url,
        extracted_by=extracted_by,
        model_version=None,
    )
