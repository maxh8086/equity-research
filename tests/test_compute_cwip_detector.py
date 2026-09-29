"""Tests for core.compute.cwip_detector."""
from datetime import date, timezone, datetime
from decimal import Decimal
import pytest
from core.compute.cwip_detector import (
    CwipEvent,
    detect_cwip_commissioning,
)
from core.resolve.cwip_commissioning import to_company_event
from core.db.models import CompanyEventSeverity, CompanyEventType

ISIN = "INE848E01016"

def _d(s: str) -> Decimal:
    return Decimal(s)

def _date(y: int, m: int, d: int) -> date:
    return date(y, m, d)

def _periods(*args) -> list:
    return [(d, Decimal(gb), Decimal(cwip)) for d, gb, cwip in args]

def test_detects_commissioning_25pct_gb_rise_cwip_fell() -> None:
    """Q3->Q4: 25% gross block rise and CWIP fell -> 1 event."""
    periods = _periods(
        (_date(2024, 3, 31), "1000", "200"),
        (_date(2024, 6, 30), "1050", "210"),
        (_date(2024, 9, 30), "1100", "220"),
        (_date(2024, 12, 31), "1375", "100"),
    )
    events = detect_cwip_commissioning(ISIN, periods)
    assert len(events) == 1
    ev = events[0]
    assert ev.isin == ISIN
    assert ev.event_date == _date(2024, 12, 31)
    assert ev.gross_block_prior == Decimal("1100")
    assert ev.gross_block_current == Decimal("1375")
    assert ev.cwip_prior == Decimal("220")
    assert ev.cwip_current == Decimal("100")
    assert ev.cwip_delta < Decimal("0")

def test_no_event_when_gb_rise_below_threshold() -> None:
    """15% gross block rise (below 20%) -> no event."""
    periods = _periods(
        (_date(2024, 3, 31), "1000", "200"),
        (_date(2024, 6, 30), "1150", "100"),
    )
    events = detect_cwip_commissioning(ISIN, periods)
    assert events == []

def test_severity_25pct_is_medium() -> None:
    periods = _periods(
        (_date(2024, 3, 31), "1000", "300"),
        (_date(2024, 6, 30), "1250", "100"),
    )
    events = detect_cwip_commissioning(ISIN, periods)
    assert len(events) == 1
    event = to_company_event(
        events[0],
        evidence_url="https://example.com/filing.pdf",
        as_of=datetime(2024, 7, 15, tzinfo=timezone.utc),
        extracted_by="test",
        content_hash="a" * 64,
        source_url="https://example.com/filing.pdf",
    )
    assert event.severity == CompanyEventSeverity.MEDIUM

def test_to_company_event_model_version_is_none() -> None:
    """model_version must be None: numbers are computed, not extracted by a model."""
    periods = _periods(
        (_date(2024, 3, 31), "1000", "300"),
        (_date(2024, 6, 30), "1250", "100"),
    )
    events = detect_cwip_commissioning(ISIN, periods)
    ev = to_company_event(
        events[0],
        evidence_url="https://example.com/filing.pdf",
        as_of=datetime(2024, 7, 15, tzinfo=timezone.utc),
        extracted_by="test",
        content_hash="e" * 64,
        source_url="https://example.com/filing.pdf",
    )
    assert ev.model_version is None
